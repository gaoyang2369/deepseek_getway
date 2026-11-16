import csv
import io
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from ..models import (
    Audit,
    Call,
    Event,
    Price,
    Student,
)
from ..operations import audit
from ..security import fail

router = APIRouter()


def filters(query, start, end, team_id, student_id, key_id, model):
    for value, column in (
        (team_id, Call.team_id),
        (student_id, Call.student_id),
        (key_id, Call.key_id),
        (model, Call.model),
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
                Call.started_at >= parsed if comparison == "start" else Call.started_at < parsed
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
    offset: int = 0,
    limit: int = 200,
):
    if offset < 0 or not 1 <= limit <= 10000:
        fail(400, "invalid_pagination")
    async with request.app.state.db() as db:
        q = filters(
            select(Call, Price.estimated, Student.name, Student.student_number)
            .join(Price, Price.id == Call.price_id)
            .join(Student, Student.id == Call.student_id),
            start,
            end,
            team_id,
            student_id,
            key_id,
            model,
        )
        rows = (await db.execute(q.order_by(Call.started_at.desc()).offset(offset).limit(limit))).all()
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
            "cost",
            "reservation",
            "estimated",
            "started_at",
            "ended_at",
        ]
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in result:
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
    return result


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
                func.count().filter(Call.status == "settled", Call.upstream_status == 200).label("success"),
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
        for r in result:
            r["failures"] = r["calls"] - r["success"]
        return result


@router.get("/events")
async def events(request: Request):
    async with request.app.state.db() as db:
        return [
            {
                k: getattr(e, k)
                for k in ("id", "kind", "team_id", "membership_id", "detail", "resolved", "created_at")
            }
            for e in (await db.scalars(select(Event).order_by(Event.created_at.desc()).limit(1000))).all()
        ]


@router.post("/events/{event_id}/resolve")
async def resolve(event_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        e = await db.get(Event, event_id)
        if not e:
            fail(404, "event_not_found")
        e.resolved = True
        audit(db, request, "event_resolve", event_id)
    return {"ok": True}


@router.get("/audit")
async def audits(request: Request):
    async with request.app.state.db() as db:
        return [
            {
                k: getattr(e, k)
                for k in ("id", "actor", "action", "target", "detail", "request_id", "created_at")
            }
            for e in (await db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(1000))).all()
        ]


@router.get("/metrics")
async def metrics(request: Request):
    async with request.app.state.db() as db:
        current = await db.scalar(
            select(func.count()).select_from(Call).where(Call.status.in_(("reserved", "running")))
        )
        pending, amount = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(Call.reservation), 0)).where(
                    Call.status == "pending"
                )
            )
        ).one()
        return {
            **request.app.state.metrics,
            "admission_batches": request.app.state.admission.stats,
            "admission_queue": request.app.state.admission.pending,
            "current_concurrency": current,
            "pending_requests": pending,
            "pending_micro_cny": amount,
        }
