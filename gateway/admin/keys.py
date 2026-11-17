from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from ..keys import issue
from ..models import (
    Membership,
    Operation,
    PersonalKey,
    Student,
    Team,
)
from ..operations import audit, operation
from ..schemas import (
    KeyIssue,
)
from ..security import fail

router = APIRouter()


@router.post("/memberships/{member_id}/keys", status_code=201)
async def issue_key(member_id: str, data: KeyIssue, request: Request):
    async with request.app.state.db() as db, db.begin():
        previous, fingerprint = await operation(
            db, data.operation_id, {"action": "key_issue", "member_id": member_id, **data.model_dump()}
        )
        if previous:
            return {**previous.result, "replayed": True, "key": None}
        member = await db.get(Membership, member_id)
        if not member:
            fail(404, "membership_not_found")
        team = await db.scalar(select(Team).where(Team.id == member.team_id).with_for_update(key_share=True))
        await db.refresh(member)
        student = await db.get(Student, member.student_id)
        if student.kind == "student":
            fail(403, "student_self_service_required")
        if not member.active or not team.active or not student.active:
            fail(400, "identity_disabled")
        if not set(data.models).issubset(team.models):
            fail(400, "models_outside_team")
        if data.revoke_key_id:
            old = await db.get(PersonalKey, data.revoke_key_id)
            if not old or old.membership_id != member.id:
                fail(400, "invalid_rotation_key")
            old.active = False
        key, raw = issue(db, request.app, member, data.models, data.expires_at)
        member.key_expires_at = data.expires_at
        result = {"key_id": key.id, "prefix": key.prefix, "membership_id": member.id}
        db.add(Operation(id=data.operation_id, fingerprint=fingerprint, result=result))
        audit(db, request, "key_issue", key.id, {"membership_id": member.id, "revoked": data.revoke_key_id})
        return {**result, "key": raw, "replayed": False}


@router.get("/keys")
async def keys(
    request: Request,
    include_deleted: bool = False,
    team_id: str | None = None,
    student_id: str | None = None,
    paginated: bool = False,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with request.app.state.db() as db:
        query = (
            select(PersonalKey, Membership, Student)
            .join(Membership, Membership.id == PersonalKey.membership_id)
            .join(Student, Student.id == Membership.student_id)
            .join(Team, Team.id == Membership.team_id)
            .order_by(PersonalKey.created_at.desc(), PersonalKey.id)
        )
        if not include_deleted:
            query = query.where(
                Membership.deleted_at.is_(None), Student.deleted_at.is_(None), Team.deleted_at.is_(None)
            )
        if team_id:
            query = query.where(Membership.team_id == team_id)
        if student_id:
            query = query.where(Membership.student_id == student_id)
        if paginated:
            total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
            query = query.offset(offset).limit(limit)
        result = [
            {
                **{
                    k: getattr(row, k)
                    for k in ("id", "membership_id", "prefix", "models", "active", "expires_at", "created_at")
                },
                **(
                    {
                        "member": {
                            "id": member.id,
                            "name": student.name,
                            "kind": student.kind,
                            "models": member.models,
                        }
                    }
                    if paginated
                    else {}
                ),
            }
            for row, member, student in (await db.execute(query)).all()
        ]
        return {"rows": result, "total": total} if paginated else result


@router.post("/keys/{key_id}/revoke")
async def revoke(key_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        key = await db.get(PersonalKey, key_id)
        if not key:
            fail(404, "key_not_found")
        member = await db.get(Membership, key.membership_id)
        await db.scalar(select(Team).where(Team.id == member.team_id).with_for_update(key_share=True))
        key.active = False
        audit(db, request, "key_revoke", key.id)
    return {"ok": True, "effect": "new_requests"}


@router.post("/memberships/{member_id}/disable")
async def disable_member(member_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        member = await db.get(Membership, member_id)
        if not member:
            fail(404, "membership_not_found")
        await db.scalar(select(Team).where(Team.id == member.team_id).with_for_update(key_share=True))
        member.active = False
        for key in (
            await db.scalars(select(PersonalKey).where(PersonalKey.membership_id == member_id))
        ).all():
            key.active = False
        audit(db, request, "membership_disable", member.id)
    return {"ok": True, "effect": "new_requests"}
