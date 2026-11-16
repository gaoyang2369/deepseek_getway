"""Persist metadata for rejected chat attempts without storing payloads or keys."""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .billing import event
from .models import Call, Membership, PersonalKey, Rejection, now
from .security import client_ip, digest

log = logging.getLogger("lab_gateway")


async def record_rejection(request, settings, factory, status):
    state = request.state
    try:
        async with factory() as db, db.begin():
            # Also guard paths where admission committed but dispatch failed before
            # the route could mark its state. Such calls already have a record.
            if await db.get(Call, state.request_id):
                return
            identity = {}
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Bearer ") and len(authorization) <= 256:
                row = (
                    await db.execute(
                        select(PersonalKey, Membership)
                        .join(Membership, Membership.id == PersonalKey.membership_id)
                        .where(PersonalKey.digest == digest(authorization[7:], settings.hmac_key))
                    )
                ).first()
                if row:
                    key, member = row
                    identity = dict(
                        key_id=key.id,
                        membership_id=member.id,
                        team_id=member.team_id,
                        student_id=member.student_id,
                    )
            reason = getattr(state, "rejection_reason", "request_rejected")
            inserted = await db.scalar(
                insert(Rejection)
                .values(
                    id=state.request_id,
                    **identity,
                    model=getattr(state, "requested_model", None),
                    reason=reason,
                    http_status=status,
                    limit_scope=getattr(state, "limit_scope", None),
                    ip=client_ip(request, settings),
                    created_at=now(),
                )
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(Rejection.id)
            )
            if inserted and identity and status in (402, 429):
                await event(
                    db,
                    reason,
                    identity["team_id"],
                    identity["membership_id"],
                    {
                        "rejection_id": state.request_id,
                        "limit_scope": getattr(state, "limit_scope", None),
                    },
                )
    except Exception:
        # Recording must not change a denial into a different client response.
        # A DB outage cannot be made durable in that same DB; expose the gap.
        request.app.state.metrics["rejection_record_failures"] += 1
        log.error("rejection_record_failed request_id=%s", state.request_id)
