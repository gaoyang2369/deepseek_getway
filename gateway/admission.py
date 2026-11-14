"""Bounded per-team microbatches; acknowledge admission only after durable commit."""

import asyncio
import json
import logging
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text

from .security import fail

log = logging.getLogger("lab_gateway")


@dataclass
class Admission:
    params: dict
    future: asyncio.Future
    abandoned: bool = False


class BudgetTurn:
    """Schedule existing transactions without a DB-connection/row-lock queue.

    Admit up to four batches before yielding to a waiting settlement. PostgreSQL
    remains authoritative; this is only a single-process latency/fairness policy.
    """

    def __init__(self):
        self.condition = asyncio.Condition()
        self.busy = False
        self.admissions = self.settlements = self.bursts = 0

    @asynccontextmanager
    async def enter(self, admission=False):
        async with self.condition:
            if admission:
                self.admissions += 1
            else:
                self.settlements += 1
            try:
                await self.condition.wait_for(
                    lambda: not self.busy
                    and (
                        (self.settlements == 0 or self.bursts < 4)
                        if admission
                        else (self.admissions == 0 or self.bursts >= 4)
                    )
                )
                self.busy = True
                self.bursts = self.bursts + 1 if admission else 0
            finally:
                if admission:
                    self.admissions -= 1
                else:
                    self.settlements -= 1
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.busy = False
                self.condition.notify_all()


class AdmissionBatcher:
    def __init__(self, factory, window=0.002, batch_size=64, capacity=2048):
        self.factory = factory
        self.window, self.batch_size, self.capacity = window, batch_size, capacity
        self.queues, self.workers = {}, {}
        self.turns = {}
        self.cleanups = set()
        self.pending = 0
        self.closing = False
        self.routes = OrderedDict()
        self.stats = {"batches": 0, "requests": 0, "max_batch_size": 0, "cleanup_failures": 0}

    def turn(self, team_id):
        return self.turns.setdefault(team_id, BudgetTurn())

    def route(self, key_digest):
        hint = self.routes.get(key_digest)
        if hint is None:
            return None
        if hint[2] <= time.monotonic():
            self.routes.pop(key_digest, None)
            return None
        self.routes.move_to_end(key_digest)
        return hint[:2]

    def remember_route(self, key_digest, team_id, member_id):
        # Routing only: every request still revalidates key, identity, policy and
        # budgets in PostgreSQL. These immutable IDs never authorize a request.
        self.routes[key_digest] = (team_id, member_id, time.monotonic() + 60)
        self.routes.move_to_end(key_digest)
        while len(self.routes) > 4096:
            self.routes.popitem(last=False)

    async def submit(self, team_id, params):
        if self.closing or self.pending >= self.capacity:
            fail(503, "admission_busy")
        queue = self.queues.setdefault(team_id, deque())
        if len(queue) >= 256:
            fail(429, "admission_queue_full")
        job = Admission(params, asyncio.get_running_loop().create_future())
        queue.append(job)
        self.pending += 1
        if team_id not in self.workers:
            self.workers[team_id] = asyncio.create_task(self._drain(team_id))
        try:
            return await asyncio.shield(job.future)
        except asyncio.CancelledError:
            job.abandoned = True
            # Cancellation may race the worker publishing a committed result.
            if job.future.done() and not job.future.cancelled() and job.future.exception() is None:
                self._cleanup(job.future.result())
            raise

    def _cleanup(self, result):
        if "call" not in result:
            return
        task = asyncio.create_task(self._release_unsent(result["call"]["id"]))
        self.cleanups.add(task)
        task.add_done_callback(self.cleanups.discard)

    async def _release_unsent(self, call_id):
        from sqlalchemy.exc import SQLAlchemyError

        from .billing import settle

        # This job never reached dispatch. On persistent DB failure its reservation
        # remains durable and recovery will conservatively mark it pending.
        for attempt in range(3):
            try:
                await settle(self.factory(), call_id, manual_cost=0, reason="not_sent")
                return
            except SQLAlchemyError:
                if attempt < 2:
                    await asyncio.sleep(0.05 * (attempt + 1))
        self.stats["cleanup_failures"] += 1
        log.error("unsent_reservation_pending request_id=%s", call_id)

    async def _execute(self, jobs):
        async with self.factory()() as db, db.begin():
            results = await db.scalar(
                text("SELECT gateway_admit_batch(CAST(:items AS jsonb))"),
                {"items": json.dumps([job.params for job in jobs])},
            )
        return results

    async def _drain(self, team_id):
        queue = self.queues[team_id]
        try:
            while queue:
                await asyncio.sleep(self.window)
                batch = [queue.popleft() for _ in range(min(len(queue), self.batch_size))]
                live = []
                for job in batch:
                    if job.abandoned:
                        job.future.cancel()
                    else:
                        live.append(job)
                try:
                    if live:
                        self.stats["batches"] += 1
                        self.stats["requests"] += len(live)
                        self.stats["max_batch_size"] = max(self.stats["max_batch_size"], len(live))
                        async with self.turn(team_id).enter(admission=True):
                            results = await self._execute(live)
                        for job, result in zip(live, results, strict=True):
                            job.future.set_result(result)
                            if job.abandoned:
                                self._cleanup(result)
                except Exception as exc:
                    for job in live:
                        if not job.future.done():
                            if job.abandoned:
                                job.future.cancel()
                            else:
                                job.future.set_exception(exc)
                finally:
                    self.pending -= len(batch)
        finally:
            self.queues.pop(team_id, None)
            self.workers.pop(team_id, None)

    async def close(self):
        self.closing = True
        # Workers are independent of cancelled clients and must finish committing
        # or rolling back before the database engine is disposed.
        if self.workers:
            await asyncio.gather(*list(self.workers.values()))
        if self.cleanups:
            await asyncio.gather(*list(self.cleanups))
