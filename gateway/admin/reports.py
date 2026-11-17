import csv
import io
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select, text

from ..models import (
    Audit,
    Call,
    Event,
    Price,
    Rejection,
    Student,
    now,
)
from ..operations import audit
from ..schemas import EventResolve
from ..security import fail
from .report_queries import report_filters, totals_columns, totals_result

router = APIRouter()


def filters(query, start, end, team_id, student_id, key_id, model, entity=Call):
    for value, column in (
        (team_id, entity.team_id),
        (student_id, entity.student_id),
        (key_id, entity.key_id),
        (model, entity.model),
    ):
        if value:
            query = query.where(column == value)
    for value, comparison in ((start, "start"), (end, "end")):
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    from ..billing import SHANGHAI

                    parsed = parsed.replace(tzinfo=SHANGHAI)
            except ValueError:
                fail(400, "invalid_time_range")
            query = query.where(
                (entity.started_at if entity is Call else entity.created_at) >= parsed
                if comparison == "start"
                else (entity.started_at if entity is Call else entity.created_at) < parsed
            )
    return query


@router.get("/requests")
@router.get("/export.csv")
async def requests(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    team_id: str | None = None,
    student_id: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    data_scope: str | None = None,
    offset: int = 0,
    limit: int = 200,
    paginated: bool = False,
    include_summary: bool = False,
):
    if offset < 0 or not 1 <= limit <= 10000:
        fail(400, "invalid_pagination")
    async with request.app.state.db() as db:
        if include_summary:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        q = report_filters(
            select(Call, Price.estimated, Student.name, Student.student_number)
            .join(Price, Price.id == Call.price_id)
            .join(Student, Student.id == Call.student_id),
            start,
            end,
            team_id,
            student_id,
            key_id,
            model,
            request_id,
            status,
            outcome,
            data_scope,
        )
        summary = (
            totals_result((await db.execute(select(*totals_columns(q.subquery().c)))).one())
            if include_summary
            else None
        )
        total = await db.scalar(select(func.count()).select_from(q.subquery())) if paginated else None
        rows = (
            await db.execute(q.order_by(Call.started_at.desc(), Call.id).offset(offset).limit(limit))
        ).all()
        result = [
            {
                **{
                    k: getattr(c, k)
                    for k in (
                        "id",
                        "student_id",
                        "membership_id",
                        "team_id",
                        "key_id",
                        "model",
                        "credential_id",
                        "price_id",
                        "status",
                        "outcome",
                        "outcome_reason",
                        "upstream_id",
                        "reason",
                        "reservation",
                        "cost",
                        "usage",
                        "ip",
                        "started_at",
                        "ended_at",
                        "admission_ms",
                        "first_delta_ms",
                        "total_ms",
                        "upstream_status",
                    )
                },
                "estimated": estimated,
                "name": name,
                "student_number": number,
            }
            for c, estimated, name, number in rows
        ]
    if request.url.path.endswith(".csv"):
        output = io.StringIO()
        columns = [
            "id",
            "name",
            "student_number",
            "team_id",
            "key_id",
            "model",
            "status",
            "outcome",
            "outcome_reason",
            "reason",
            "upstream_id",
            "cost",
            "reservation",
            "estimated",
            "started_at",
            "ended_at",
            "input_tokens",
            "output_tokens",
            "cache_hit_tokens",
        ]
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in result:
            usage = row["usage"] or {}
            row.update(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                cache_hit_tokens=usage.get("prompt_cache_hit_tokens"),
            )
            values = []
            for col in columns:
                value = str(row[col]) if row[col] is not None else ""
                if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
        )
    return (
        {"rows": result, "total": total, **({"summary": summary} if include_summary else {})}
        if paginated
        else result
    )


@router.get("/statistics")
async def statistics(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    team_id: str | None = None,
    student_id: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
):
    async with request.app.state.db() as db:
        q = filters(
            select(
                Call.team_id,
                Call.student_id,
                Call.key_id,
                Call.model,
                func.count().label("calls"),
                func.count().filter(Call.outcome == "success").label("success"),
                func.count().filter(Call.outcome == "upstream_failure").label("failures"),
                func.count().filter(Call.outcome == "in_progress").label("in_progress"),
                func.count().filter(Call.outcome == "unknown").label("unknown"),
                func.count().filter(Call.outcome == "client_disconnected").label("client_disconnected"),
                func.count().filter(Call.outcome == "not_sent").label("not_sent"),
                func.count().filter(Call.status == "pending").label("pending"),
                func.coalesce(func.sum(Call.cost), 0).label("cost"),
                func.coalesce(func.sum(Call.reservation).filter(Call.status != "settled"), 0).label(
                    "reserved"
                ),
                func.coalesce(func.sum(Call.usage["prompt_tokens"].as_integer()), 0).label("input_tokens"),
                func.coalesce(func.sum(Call.usage["completion_tokens"].as_integer()), 0).label(
                    "output_tokens"
                ),
                func.coalesce(func.sum(Call.usage["prompt_cache_hit_tokens"].as_integer()), 0).label(
                    "cache_hit_tokens"
                ),
            ),
            start,
            end,
            team_id,
            student_id,
            key_id,
            model,
        )
        result = [
            dict(r._mapping)
            for r in (
                await db.execute(q.group_by(Call.team_id, Call.student_id, Call.key_id, Call.model))
            ).all()
        ]
        return result


@router.get("/rejections")
async def rejections(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    team_id: str | None = None,
    student_id: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    offset: int = 0,
    limit: int = 100,
):
    if offset < 0 or not 1 <= limit <= 10000:
        fail(400, "invalid_pagination")
    q = filters(select(Rejection), start, end, team_id, student_id, key_id, model, Rejection)
    if reason:
        q = q.where(Rejection.reason == reason)
    if request_id:
        q = q.where(Rejection.id == request_id)
    async with request.app.state.db() as db:
        total = await db.scalar(select(func.count()).select_from(q.subquery()))
        rows = (
            await db.scalars(
                q.order_by(Rejection.created_at.desc(), Rejection.id).offset(offset).limit(limit)
            )
        ).all()
        return {
            "total": total,
            "rows": [{c.name: getattr(r, c.name) for c in Rejection.__table__.columns} for r in rows],
        }


def event_severity(kind):
    if kind in ("pending_usage", "recovery_pending", "reservation_exceeded"):
        return "action"
    return "info" if kind == "new_source" else "warning"


@router.get("/events")
async def events(
    request: Request,
    resolved: bool | None = None,
    team_id: str | None = None,
    offset: int = 0,
    limit: int = 1000,
    paginated: bool = False,
):
    if offset < 0 or not 1 <= limit <= 1000:
        fail(400, "invalid_pagination")
    q = select(Event)
    if resolved is not None:
        q = q.where(Event.resolved == resolved)
    if team_id:
        q = q.where(Event.team_id == team_id)
    async with request.app.state.db() as db:
        total = await db.scalar(select(func.count()).select_from(q.subquery())) if paginated else None
        rows = [
            {
                **{
                    k: getattr(e, k)
                    for k in (
                        "id",
                        "kind",
                        "team_id",
                        "membership_id",
                        "detail",
                        "resolved",
                        "created_at",
                        "last_seen_at",
                        "occurrences",
                        "resolved_at",
                        "resolved_by",
                        "resolution",
                    )
                },
                "severity": event_severity(e.kind),
            }
            for e in (
                await db.scalars(q.order_by(Event.last_seen_at.desc(), Event.id).offset(offset).limit(limit))
            ).all()
        ]
        return {"rows": rows, "total": total} if paginated else rows


@router.post("/events/{event_id}/resolve")
async def resolve(event_id: str, data: EventResolve, request: Request):
    async with request.app.state.db() as db, db.begin():
        # Read the request before locking the event: settlement locks in this
        # same order. Otherwise a concurrent reconciliation could deadlock.
        e = await db.get(Event, event_id)
        if not e:
            fail(404, "event_not_found")
        call_id = e.detail.get("request_id")
        if e.kind == "pending_usage" and call_id:
            call = await db.get(Call, call_id, with_for_update=True)
            if call and call.status != "settled":
                fail(409, "event_requires_reconciliation")
        if e.kind == "recovery_pending":
            if await db.scalar(select(Call.id).where(Call.status == "pending").limit(1)):
                fail(409, "event_requires_reconciliation")
        await db.refresh(e, with_for_update=True)
        if not e.resolved:
            e.resolved, e.resolved_at = True, now()
            e.resolved_by = request.app.state.settings.admin_name
            e.resolution = data.reason
            audit(db, request, "event_resolve", event_id, {"reason": data.reason})
    return {"ok": True}


@router.get("/audit")
async def audits(request: Request, offset: int = 0, limit: int = 1000, paginated: bool = False):
    if offset < 0 or not 1 <= limit <= 1000:
        fail(400, "invalid_pagination")
    async with request.app.state.db() as db:
        total = await db.scalar(select(func.count()).select_from(Audit)) if paginated else None
        rows = [
            {
                k: getattr(e, k)
                for k in ("id", "actor", "action", "target", "detail", "request_id", "created_at")
            }
            for e in (
                await db.scalars(
                    select(Audit).order_by(Audit.created_at.desc(), Audit.id).offset(offset).limit(limit)
                )
            ).all()
        ]
        return {"rows": rows, "total": total} if paginated else rows


@router.get("/metrics")
async def metrics(request: Request):
    async with request.app.state.db() as db:
        current = await db.scalar(
            select(func.count()).select_from(Call).where(Call.status.in_(("reserved", "running")))
        )
        pending = await db.scalar(select(func.count()).select_from(Call).where(Call.status == "pending"))
        return {
            **request.app.state.metrics,
            "admission_batches": request.app.state.admission.stats,
            "admission_queue": request.app.state.admission.pending,
            "current_concurrency": current,
            "pending_requests": pending,
            # No reservation estimate exists; unknown cost is not a zero bill.
            "pending_micro_cny": None if pending else 0,
        }
