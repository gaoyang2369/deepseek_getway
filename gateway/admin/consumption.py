"""Administrative consumption views. Does not participate in admission or settlement."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, StrictBool
from sqlalchemy import func, select, text

from ..billing import fee, period_bounds
from ..models import Audit, BudgetPeriod, Call, Membership, Price, Student, Team, now
from ..operations import audit
from ..schemas import Reconcile
from ..security import fail
from .report_queries import report_filters, test_team_ids, totals_columns, totals_result

router = APIRouter()


@router.get("/consumption")
async def consumption(
    request: Request,
    group_by: Literal["team", "member", "model", "key"] = "team",
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
    include_trend: bool = True,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    selected = report_filters(
        select(Call), start, end, team_id, student_id, key_id, model, request_id, status, outcome, data_scope
    ).cte("selected_calls")
    c = selected.c
    dimension = {"team": c.team_id, "member": c.student_id, "model": c.model, "key": c.key_id}[group_by]
    grouped = select(dimension.label("id"), *totals_columns(c)).group_by(dimension).subquery()
    day = func.date(func.timezone("Asia/Shanghai", c.started_at))
    async with request.app.state.db() as db:
        # One consistent snapshot for the cards, grouped totals and daily trend.
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        summary = totals_result((await db.execute(select(*totals_columns(c)))).one())
        total = await db.scalar(select(func.count()).select_from(grouped))
        rows = [
            dict(r._mapping)
            for r in (
                await db.execute(
                    select(grouped).order_by(grouped.c.cost.desc(), grouped.c.id).offset(offset).limit(limit)
                )
            ).all()
        ]
        trend = []
        if include_trend:
            trend = [
                dict(r._mapping)
                for r in (
                    await db.execute(
                        select(
                            day.label("day"),
                            func.coalesce(func.sum(c.cost).filter(c.status == "settled"), 0).label("cost"),
                            func.count().filter(c.status == "pending").label("pending"),
                        )
                        .group_by(day)
                        .order_by(day)
                    )
                ).all()
            ]
    return {
        "summary": summary,
        "rows": rows,
        "total": total,
        "trend": trend,
        "group_by": group_by,
        "timezone": "Asia/Shanghai",
        "currency": "CNY",
        "cost_basis": "gateway_ledger_not_upstream_bill",
    }


@router.get("/requests/{call_id}")
async def request_detail(call_id: str, request: Request):
    async with request.app.state.db() as db:
        row = (
            await db.execute(
                select(Call, Price, Student, Team)
                .join(Price, Price.id == Call.price_id)
                .join(Student, Student.id == Call.student_id)
                .join(Team, Team.id == Call.team_id)
                .where(Call.id == call_id)
            )
        ).first()
        if not row:
            fail(404, "request_not_found")
        call, price, person, team = row
        reconciliation = (
            await db.execute(
                select(Audit.actor, Audit.created_at, Audit.detail)
                .where(Audit.action == "reconcile", Audit.target == call_id)
                .order_by(Audit.created_at.desc(), Audit.id.desc())
                .limit(1)
            )
        ).first()
        verification = (
            await db.execute(
                select(Audit.actor, Audit.created_at, Audit.detail)
                .where(Audit.action == "bill_verification", Audit.target == call_id)
                .order_by(Audit.created_at.desc(), Audit.id.desc())
                .limit(1)
            )
        ).first()
        verified = bool(
            verification and call.status == "settled" and verification.detail.get("cost") == call.cost
        )
        usage = call.usage
        parts = []
        calculated = None
        rounding = None
        if usage is not None:
            for name, tokens, rate in (
                ("input_miss", usage["prompt_tokens"] - usage["prompt_cache_hit_tokens"], price.input_miss),
                ("input_hit", usage["prompt_cache_hit_tokens"], price.input_hit),
                ("output", usage["completion_tokens"], price.output),
            ):
                # Decimal strings retain sub-micro-CNY precision; round only the request total.
                parts.append(
                    {
                        "kind": name,
                        "tokens": tokens,
                        "rate": rate,
                        "amount_yuan": format(Decimal(tokens * rate) / Decimal(10**12), "f"),
                    }
                )
            calculated = fee(
                usage["prompt_tokens"], usage["completion_tokens"], usage["prompt_cache_hit_tokens"], price
            )
            rounding = format(
                Decimal(calculated) / 10**6 - sum(Decimal(part["amount_yuan"]) for part in parts), "f"
            )
        return {
            **{col.name: getattr(call, col.name) for col in Call.__table__.columns},
            "name": person.name,
            "student_number": person.student_number,
            "team_name": team.name,
            "team_deleted": team.deleted_at is not None,
            "price": {
                k: getattr(price, k)
                for k in (
                    "id",
                    "version",
                    "model",
                    "upstream_model",
                    "input_miss",
                    "input_hit",
                    "output",
                    "estimated",
                    "note",
                    "starts_at",
                    "ends_at",
                )
            },
            "cost_basis": "manual_reconciliation" if reconciliation else "rate_estimate",
            # A rate's `estimated=false` flag is not evidence of upstream bill reconciliation.
            "upstream_verified": verified,
            "bill_verification": dict(verification._mapping) if verified else None,
            "reconciliation": dict(reconciliation._mapping) if reconciliation else None,
            "components": parts,
            "calculated_cost": calculated,
            "rounding_yuan": rounding,
        }


class TestLabel(BaseModel):
    is_test: StrictBool


@router.get("/report-labels")
async def report_labels(request: Request):
    async with request.app.state.db() as db:
        return {"test_team_ids": list((await db.scalars(test_team_ids())).all())}


@router.put("/teams/{team_id}/report-label")
async def label_team(team_id: str, data: TestLabel, request: Request):
    async with request.app.state.db() as db, db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 9))"), {"id": team_id})
        if not await db.get(Team, team_id):
            fail(404, "team_not_found")
        audit(db, request, "report_test_label", team_id, {"is_test": data.is_test})
    return {"ok": True}


@router.post("/requests/{call_id}/bill-verification")
async def verify_bill(call_id: str, data: Reconcile, request: Request):
    """Record explicit evidence, without changing ledger amounts or request execution."""
    async with request.app.state.db() as db, db.begin():
        call = await db.get(Call, call_id)
        if not call:
            fail(404, "request_not_found")
        if call.status != "settled" or call.cost != data.cost:
            fail(409, "bill_amount_mismatch")
        audit(db, request, "bill_verification", call_id, {"cost": data.cost, "reason": data.reason})
    return {"ok": True}


@router.get("/consumption-options")
async def consumption_options(request: Request):
    """Include historical calls even when a model is no longer in the price catalogue."""
    async with request.app.state.db() as db:
        models = (await db.scalars(select(Price.model).union(select(Call.model)).order_by("model"))).all()
    return {"models": models}


@router.get("/budget-status")
async def budget_status(
    request: Request,
    owner_type: Literal["team", "membership"] = "team",
    student_id: str | None = None,
    include_month_cost: bool = False,
):
    """Current configured budgets, including unused owners, without creating ledger periods."""
    stamp = now()
    async with request.app.state.db() as db:
        if owner_type == "team":
            owners = [
                (t, t.id, t.name, t.active)
                for t in (
                    await db.scalars(
                        select(Team).where(Team.deleted_at.is_(None)).order_by(Team.name, Team.id)
                    )
                ).all()
            ]
        else:
            query = (
                select(Membership, Team, Student)
                .join(Team, Team.id == Membership.team_id)
                .join(Student, Student.id == Membership.student_id)
                .where(
                    Membership.deleted_at.is_(None), Team.deleted_at.is_(None), Student.deleted_at.is_(None)
                )
                .order_by(Student.name, Membership.id)
            )
            if student_id:
                query = query.where(Membership.student_id == student_id)
            owners = [
                (m, t.id, s.name + " / " + t.name, m.active and t.active and s.active)
                for m, t, s in (await db.execute(query)).all()
            ]
        periods = (
            {
                (p.owner_id, p.starts_at): p
                for p in (
                    await db.scalars(
                        select(BudgetPeriod).where(
                            BudgetPeriod.owner_type == owner_type,
                            BudgetPeriod.owner_id.in_([o.id for o, _, _, _ in owners]),
                            BudgetPeriod.starts_at <= stamp,
                            BudgetPeriod.ends_at > stamp,
                        )
                    )
                ).all()
            }
            if owners
            else {}
        )
        # A single aggregate for all projects; the current-month reporting total
        # uses the same started_at / settled-cost definition as /consumption.
        month_start, month_end = period_bounds(stamp, "month")
        month_costs = (
            dict(
                (
                    await db.execute(
                        select(Call.team_id, func.coalesce(func.sum(Call.cost), 0))
                        .where(
                            Call.status == "settled",
                            Call.started_at >= month_start,
                            Call.started_at < month_end,
                        )
                        .group_by(Call.team_id)
                    )
                ).all()
            )
            if owner_type == "team" and include_month_cost
            else {}
        )
        test_ids = set((await db.scalars(test_team_ids())).all())
        result = []
        for owner, team_id, name, active in owners:
            start, end = period_bounds(stamp, owner.period)
            period = periods.get((owner.id, start))
            limit, spent = (period.limit, period.spent) if period else (owner.budget, 0)
            result.append(
                {
                    **(
                        {"month_cost": month_costs.get(team_id, 0)}
                        if owner_type == "team" and include_month_cost
                        else {}
                    ),
                    "owner_type": owner_type,
                    "owner_id": owner.id,
                    "is_test": team_id in test_ids,
                    "team_id": team_id,
                    "name": name,
                    "active": active,
                    "period": owner.period,
                    "limit": limit,
                    "spent": spent,
                    "available": limit - spent,
                    "starts_at": start,
                    "ends_at": end,
                }
            )
        return result
