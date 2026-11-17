from fastapi import APIRouter, Request
from sqlalchemy import case, delete, or_, select

from ..billing import get_period, settle
from ..models import (
    BudgetPeriod,
    Call,
    Event,
    Ledger,
    Membership,
    Rejection,
    Team,
    now,
)
from ..operations import audit
from ..schemas import (
    BudgetUpdate,
    Reconcile,
    TestUsageClear,
)
from ..security import fail

router = APIRouter()


@router.post("/budgets/{owner_type}/{owner_id}")
async def update_budget(owner_type: str, owner_id: str, data: BudgetUpdate, request: Request):
    model = {"team": Team, "membership": Membership}.get(owner_type)
    if model is None:
        fail(400, "invalid_budget_owner")
    async with request.app.state.db() as db, db.begin():
        owner = await db.get(model, owner_id)
        if not owner:
            fail(404, "owner_not_found")
        team_id = owner.id if owner_type == "team" else owner.team_id
        await db.scalar(select(Team).where(Team.id == team_id).with_for_update(key_share=True))
        await db.refresh(owner)
        if owner.deleted_at:
            fail(404, "owner_not_found")
        p = await get_period(db, owner_type, owner, now())
        await db.refresh(p, with_for_update=True)
        old = owner.budget
        owner.budget, owner.policy_version = data.amount, owner.policy_version + 1
        delta = data.amount - p.limit
        p.limit, p.policy_version = data.amount, owner.policy_version
        db.add(
            Ledger(
                operation=f"adjust:{request.state.request_id}",
                period_id=p.id,
                kind="limit_adjust",
                amount=delta,
                reason=data.reason,
            )
        )
        audit(
            db,
            request,
            "budget_update",
            owner.id,
            {"before": old, "after": data.amount, "reason": data.reason},
        )
        return {
            "limit": p.limit,
            "spent": p.spent,
            "reserved": p.reserved,
            "available": p.limit - p.spent,
        }


@router.get("/budgets")
async def budgets(request: Request):
    async with request.app.state.db() as db:
        return [
            {
                **{
                    k: getattr(p, k)
                    for k in (
                        "id",
                        "owner_type",
                        "owner_id",
                        "starts_at",
                        "ends_at",
                        "limit",
                        "spent",
                        "reserved",
                    )
                },
                "available": p.limit - p.spent,
                "team_id": team_id,
            }
            for p, team_id in (
                await db.execute(
                    select(
                        BudgetPeriod,
                        case(
                            (BudgetPeriod.owner_type == "team", BudgetPeriod.owner_id),
                            else_=Membership.team_id,
                        ),
                    )
                    .outerjoin(
                        Membership,
                        (BudgetPeriod.owner_type == "membership") & (Membership.id == BudgetPeriod.owner_id),
                    )
                    .order_by(BudgetPeriod.starts_at.desc())
                    .limit(2000)
                )
            ).all()
        ]


def test_cleanup_enabled(settings):
    return not settings.production and settings.upstream_url in (
        "http://mock:8080/v1",
        "http://127.0.0.1:18082/v1",
    )


@router.get("/config")
async def admin_config(request: Request):
    settings = request.app.state.settings
    return {
        "demo": not settings.production,
        "test_cleanup_enabled": test_cleanup_enabled(settings),
        "initial_password_mode": "random" if settings.production else "student_number",
    }


@router.post("/teams/{team_id}/clear-test-usage")
async def clear_test_usage(team_id: str, data: TestUsageClear, request: Request):
    if not test_cleanup_enabled(request.app.state.settings):
        fail(403, "test_cleanup_requires_mock_environment")
    async with request.app.state.admission.turn(team_id).enter():
        async with request.app.state.db() as db, db.begin():
            team = await db.scalar(select(Team).where(Team.id == team_id).with_for_update(key_share=True))
            if not team:
                fail(404, "team_not_found")
            if data.confirm_name != team.name:
                fail(400, "confirmation_name_mismatch")
            calls = (
                await db.execute(
                    select(Call.id, Call.status).where(Call.team_id == team_id).with_for_update()
                )
            ).all()
            if any(status in ("reserved", "running") for _, status in calls):
                fail(409, "test_cleanup_requests_in_progress")
            periods = select(BudgetPeriod.id).where(
                or_(
                    (BudgetPeriod.owner_type == "team") & (BudgetPeriod.owner_id == team_id),
                    (BudgetPeriod.owner_type == "membership")
                    & BudgetPeriod.owner_id.in_(select(Membership.id).where(Membership.team_id == team_id)),
                )
            )
            # Both sides of a call's budget accounting are removed in one transaction.
            ledgers = await db.execute(delete(Ledger).where(Ledger.period_id.in_(periods)))
            await db.execute(delete(Call).where(Call.team_id == team_id))
            removed_periods = await db.execute(delete(BudgetPeriod).where(BudgetPeriod.id.in_(periods)))
            await db.execute(delete(Event).where(Event.team_id == team_id))
            rejections = await db.execute(delete(Rejection).where(Rejection.team_id == team_id))
            counts = {
                "calls": len(calls),
                "rejections": rejections.rowcount,
                "budget_periods": removed_periods.rowcount,
                "ledger_entries": ledgers.rowcount,
            }
            audit(db, request, "test_usage_clear", team_id, {"name": team.name, **counts})
    return {"ok": True, "removed": counts, "configuration_preserved": True}


@router.post("/requests/{call_id}/reconcile")
async def reconcile(call_id: str, data: Reconcile, request: Request):
    async with request.app.state.db() as db:
        call = await db.get(Call, call_id)
        if not call or call.status != "pending":
            fail(409, "request_not_pending")
    changed = await settle(
        request.app.state.db,
        call_id,
        reason="manual_reconciliation",
        manual_cost=data.cost,
        thresholds=request.app.state.settings.budget_thresholds,
        manual_audit={
            "actor": request.app.state.settings.admin_name,
            "action": "reconcile",
            "target": call_id,
            "detail": {"cost": data.cost, "reason": data.reason},
            "request_id": request.state.request_id,
        },
    )
    return {"applied": changed}
