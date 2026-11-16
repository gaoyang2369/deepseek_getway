"""Personal key authentication and issuance; callers own their transaction."""

from sqlalchemy import select

from .models import Membership, PersonalKey, Student, Team, now, uid
from .security import digest, fail, new_key


async def authenticate(db, authorization, settings):
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 256:
        fail(401, "invalid_api_key")
    row = (
        await db.execute(
            select(PersonalKey, Membership, Student, Team)
            .join(Membership, Membership.id == PersonalKey.membership_id)
            .join(Student, Student.id == Membership.student_id)
            .join(Team, Team.id == Membership.team_id)
            .where(PersonalKey.digest == digest(authorization[7:], settings.hmac_key))
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        fail(401, "invalid_api_key")
    key, member, student, team = row
    if not key.active or (key.expires_at and key.expires_at <= now()):
        fail(401, "invalid_api_key")
    if not member.active or not student.active or not team.active:
        fail(403, "identity_disabled")
    return key, member, student, team


def issue(db, app, member, models, expires):
    if not set(models).issubset(member.models):
        fail(400, "models_outside_membership")
    if expires and expires <= now():
        fail(400, "expiry_in_past")
    raw = new_key()
    key = PersonalKey(
        id=uid(),
        membership_id=member.id,
        digest=digest(raw, app.state.settings.hmac_key),
        prefix=raw[:12],
        models=models,
        expires_at=expires,
    )
    db.add(key)
    return key, raw
