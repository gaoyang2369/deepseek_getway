"""Unified administrative member directory; account writes retain their existing routes."""

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, or_, select

from ..billing import period_bounds
from ..models import Call, Membership, Student, StudentAccount, Team, now

router = APIRouter()


@router.get("/people")
async def people(
    request: Request,
    q: str = Query("", max_length=100),
    team_id: str | None = None,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    paginated: bool = True,
):
    start, end = period_bounds(now(), "month")
    spending = select(Call.student_id, func.sum(Call.cost).label("cost")).where(
        Call.started_at >= start, Call.started_at < end, Call.status == "settled"
    )
    if team_id:
        spending = spending.where(Call.team_id == team_id)
    spending = spending.group_by(Call.student_id).subquery()
    query = (
        select(Student, StudentAccount.must_change_password, func.coalesce(spending.c.cost, 0))
        .outerjoin(StudentAccount, StudentAccount.student_id == Student.id)
        .outerjoin(spending, spending.c.student_id == Student.id)
        .where(Student.deleted_at.is_(None))
    )
    if term := q.strip():
        query = query.where(
            or_(
                Student.name.icontains(term, autoescape=True),
                Student.student_number.icontains(term, autoescape=True),
            )
        )
    if team_id:
        query = query.where(
            Student.id.in_(
                select(Membership.student_id).where(
                    Membership.team_id == team_id, Membership.deleted_at.is_(None)
                )
            )
        )
    async with request.app.state.db() as db:
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await db.execute(query.order_by(Student.student_number, Student.id).offset(offset).limit(limit))
        ).all()
        ids = [person.id for person, _, _ in rows]
        memberships = (
            (
                await db.execute(
                    select(Membership.student_id, Team.id, Team.name)
                    .join(Team, Team.id == Membership.team_id)
                    .where(
                        Membership.student_id.in_(ids),
                        Membership.deleted_at.is_(None),
                        Team.deleted_at.is_(None),
                    )
                    .order_by(Team.name, Team.id)
                )
            ).all()
            if ids
            else []
        )
        return {
            "total": total,
            "rows": [
                {
                    "student_id": p.id,
                    "name": p.name,
                    "student_number": p.student_number,
                    "kind": p.kind,
                    "owner": p.owner,
                    "active": p.active,
                    "must_change_password": password,
                    "month_cost": cost,
                    "teams": [{"id": tid, "name": name} for sid, tid, name in memberships if sid == p.id],
                }
                for p, password, cost in rows
            ],
        }
