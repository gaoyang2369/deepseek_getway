from fastapi import APIRouter, Request
from sqlalchemy import select, update

from ..accounts import remove_memberships
from ..models import (
    Credential,
    Membership,
    Team,
    now,
    uid,
)
from ..operations import audit
from ..schemas import (
    CredentialRotate,
    TeamCreate,
    TeamPatch,
)
from ..security import fail

router = APIRouter()


def public_team(t):
    return {
        k: getattr(t, k)
        for k in (
            "id",
            "name",
            "code",
            "active",
            "models",
            "budget",
            "period",
            "rpm",
            "concurrency",
            "max_output",
            "deleted_at",
        )
    }


@router.get("/teams")
async def teams(request: Request, include_deleted: bool = False):
    async with request.app.state.db() as db:
        query = select(Team).order_by(Team.code)
        if not include_deleted:
            query = query.where(Team.deleted_at.is_(None))
        return [public_team(t) for t in (await db.scalars(query)).all()]


@router.post("/teams", status_code=201)
async def create_team(data: TeamCreate, request: Request):
    async with request.app.state.db() as db, db.begin():
        values = data.model_dump(exclude={"upstream_key"})
        values["code"] = values["code"] or "team_" + uid().replace("-", "")
        t = Team(**values)
        db.add(t)
        await db.flush()
        db.add(
            Credential(
                team_id=t.id,
                version=1,
                ciphertext=request.app.state.cipher.encrypt(data.upstream_key.encode()).decode(),
            )
        )
        audit(db, request, "team_create", t.id, {"code": t.code, "budget": t.budget})
        return public_team(t)


@router.patch("/teams/{team_id}")
async def update_team(team_id: str, data: TeamPatch, request: Request):
    async with request.app.state.db() as db, db.begin():
        t = await db.scalar(select(Team).where(Team.id == team_id).with_for_update(key_share=True))
        if not t or t.deleted_at:
            fail(404, "team_not_found")
        changes = data.model_dump(exclude_none=True)
        before = {k: getattr(t, k) for k in changes}
        for k, v in changes.items():
            setattr(t, k, v)
        audit(db, request, "team_update", t.id, {"before": before, "after": changes})
        return public_team(t)


@router.post("/teams/{team_id}/credential")
async def rotate_credential(team_id: str, data: CredentialRotate, request: Request):
    async with request.app.state.db() as db, db.begin():
        t = await db.scalar(select(Team).where(Team.id == team_id).with_for_update(key_share=True))
        if not t or t.deleted_at:
            fail(404, "team_not_found")
        credentials = (await db.scalars(select(Credential).where(Credential.team_id == team_id))).all()
        version = max((c.version for c in credentials), default=0) + 1
        for c in credentials:
            c.active = False
        db.add(
            Credential(
                team_id=team_id,
                version=version,
                ciphertext=request.app.state.cipher.encrypt(data.upstream_key.encode()).decode(),
            )
        )
        audit(db, request, "credential_rotate", team_id, {"version": version})
        return {"version": version, "credential": "********"}


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        team = await db.scalar(select(Team).where(Team.id == team_id).with_for_update(key_share=True))
        if not team:
            fail(404, "team_not_found")
        if not team.deleted_at:
            team.active, team.deleted_at = False, now()
            await remove_memberships(db, Membership.team_id == team.id, team.deleted_at)
            await db.execute(update(Credential).where(Credential.team_id == team.id).values(active=False))
            audit(db, request, "team_delete", team.id, {"code": team.code, "history_retained": True})
    return {"ok": True, "history_retained": True}


@router.delete("/memberships/{member_id}")
async def delete_member(member_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        member = await db.get(Membership, member_id)
        if not member:
            fail(404, "membership_not_found")
        await db.scalar(select(Team).where(Team.id == member.team_id).with_for_update(key_share=True))
        await db.refresh(member)
        if not member.deleted_at:
            await remove_memberships(db, Membership.id == member.id, now())
            audit(db, request, "membership_delete", member.id, {"history_retained": True})
    return {"ok": True, "history_retained": True}
