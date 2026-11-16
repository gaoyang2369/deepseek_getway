"""Pre-provisioned student accounts. Never permit self-registration or budget edits."""

import secrets
from collections import defaultdict
from datetime import timedelta

import anyio
from argon2 import PasswordHasher
from fastapi import APIRouter, Request, Response
from sqlalchemy import delete, func, select

from .accounts import password_matches
from .billing import period_bounds
from .keys import issue
from .models import (
    Call,
    Membership,
    Operation,
    PersonalKey,
    Student,
    StudentAccount,
    StudentSession,
    Team,
    now,
)
from .operations import audit, operation
from .schemas import Login, PasswordChange, StudentKeyIssue
from .security import check_origin, client_ip, digest, fail

router = APIRouter(prefix="/student/api")
COOKIE = "lab_student_session"
HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))


async def rate_limit(request, scope, identity, limit=10, seconds=300):
    settings = request.app.state.settings
    for label, value, maximum in (
        ("ip", client_ip(request, settings), max(limit * 3, 30)),
        ("account", identity, limit),
    ):
        count = await request.app.state.redis.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            f"student:{scope}:{label}:{digest(value, settings.hmac_key)}",
            seconds,
        )
        if count > maximum:
            fail(429, "student_rate_limit")


async def start_session(db, request, response, account):
    settings = request.app.state.settings
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        StudentSession(
            digest=digest(token, settings.hmac_key),
            student_id=account.student_id,
            auth_version=account.auth_version,
            csrf_digest=digest(csrf, settings.hmac_key),
            expires_at=now() + timedelta(seconds=settings.session_seconds),
        )
    )
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.production,
        samesite="strict",
        max_age=settings.session_seconds,
        path="/student",
    )
    return {"csrf_token": csrf, "must_change_password": account.must_change_password}


async def identity(db, request, *, lock=False, allow_initial=False):
    settings = request.app.state.settings
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 200:
        fail(401, "student_login_required")
    session = await db.get(StudentSession, digest(token, settings.hmac_key))
    if not session or session.expires_at <= now():
        fail(401, "student_login_required")
    query = select(StudentAccount).where(StudentAccount.student_id == session.student_id)
    account = await db.scalar(query.with_for_update() if lock else query)
    student = await db.get(Student, session.student_id)
    if (
        not account
        or not student
        or not student.active
        or student.kind != "student"
        or account.auth_version != session.auth_version
    ):
        fail(401, "student_login_required")
    if request.method != "GET":
        check_origin(request)
        supplied = digest(request.headers.get("x-csrf-token", ""), settings.hmac_key)
        if not secrets.compare_digest(supplied, session.csrf_digest):
            fail(403, "csrf_required")
    if account.must_change_password and not allow_initial:
        fail(403, "password_change_required")
    return account, student, session


@router.post("/login")
async def login(data: Login, request: Request, response: Response):
    check_origin(request)
    await rate_limit(request, "login", data.username)
    async with request.app.state.db() as db:
        row = (
            await db.execute(
                select(StudentAccount, Student)
                .join(Student, Student.id == StudentAccount.student_id)
                .where(Student.student_number == data.username, Student.deleted_at.is_(None))
            )
        ).first()
        encoded = row[0].password_hash if row else DUMMY_HASH
        valid = await password_matches(encoded, data.password)
    if not valid or not row or not row[1].active or row[1].kind != "student":
        fail(401, "invalid_login")
    # Reset/change may race with slow Argon2: lock and recheck the exact verified hash.
    async with request.app.state.db() as db, db.begin():
        account = await db.scalar(
            select(StudentAccount).where(StudentAccount.student_id == row[0].student_id).with_for_update()
        )
        student = await db.get(Student, account.student_id)
        if account.password_hash != encoded or not student.active:
            fail(401, "invalid_login")
        audit(db, request, "student_login", account.student_id, actor="student:" + account.student_id)
        return await start_session(db, request, response, account)


@router.get("/me")
async def me(request: Request):
    async with request.app.state.db() as db:
        account, student, _ = await identity(db, request, allow_initial=True)
        result = {
            "name": student.name,
            "student_number": student.student_number,
            "must_change_password": account.must_change_password,
            "base_url": request.app.state.settings.public_url + "/v1",
            "support_email": request.app.state.settings.support_email,
        }
        if account.must_change_password:
            return result
        memberships = (
            await db.execute(
                select(Membership, Team)
                .join(Team, Team.id == Membership.team_id)
                .where(
                    Membership.student_id == student.id,
                    Membership.deleted_at.is_(None),
                    Team.deleted_at.is_(None),
                )
                .order_by(Team.code)
            )
        ).all()
        member_ids = [member.id for member, _ in memberships]
        month_start, month_end = period_bounds(now(), "month")
        # Show monthly settled usage regardless of daily/monthly admission limits.
        # Attribute reconciliation to the original request month, as accounting does.
        monthly_spent = dict(
            (
                await db.execute(
                    select(Call.team_id, func.coalesce(func.sum(Call.cost), 0))
                    .where(
                        Call.student_id == student.id,
                        Call.team_id.in_([team.id for _, team in memberships]),
                        Call.status == "settled",
                        Call.started_at >= month_start,
                        Call.started_at < month_end,
                    )
                    .group_by(Call.team_id)
                )
            ).all()
        )
        keys_by_member = defaultdict(list)
        for key in (
            await db.scalars(
                select(PersonalKey)
                .where(PersonalKey.membership_id.in_(member_ids))
                .order_by(PersonalKey.created_at.desc())
            )
        ).all():
            keys_by_member[key.membership_id].append(key)
        result["memberships"] = []
        for member, team in memberships:
            keys = keys_by_member[member.id]
            result["memberships"].append(
                {
                    "id": member.id,
                    "team": team.name,
                    "active": member.active and team.active,
                    "models": sorted(set(member.models) & set(team.models)),
                    "max_output": team.max_output,
                    "key_expires_at": member.key_expires_at,
                    "spent": monthly_spent.get(team.id, 0),
                    "keys": [
                        {"id": k.id, "prefix": k.prefix, "active": k.active, "expires_at": k.expires_at}
                        for k in keys
                    ],
                }
            )
        return result


@router.post("/password")
async def password(data: PasswordChange, request: Request, response: Response):
    async with request.app.state.db() as db, db.begin():
        account, student, _ = await identity(db, request, lock=True, allow_initial=True)
        await rate_limit(request, "password", student.id)
        if not await password_matches(account.password_hash, data.current_password):
            fail(401, "invalid_current_password")
        if (
            data.new_password == student.student_number
            or data.new_password == data.current_password
            or len(set(data.new_password)) < 4
        ):
            fail(400, "weak_or_unchanged_password")
        account.password_hash = await anyio.to_thread.run_sync(HASHER.hash, data.new_password)
        account.must_change_password = False
        account.auth_version += 1
        await db.execute(delete(StudentSession).where(StudentSession.student_id == student.id))
        audit(db, request, "student_password_change", student.id, actor="student:" + student.id)
        return await start_session(db, request, response, account)


@router.post("/keys", status_code=201)
async def issue_key(data: StudentKeyIssue, request: Request):
    async with request.app.state.db() as db, db.begin():
        account, student, _ = await identity(db, request, lock=True)
        await rate_limit(request, "keys", student.id, limit=30, seconds=3600)
        previous, fingerprint = await operation(
            db, data.operation_id, {"action": "student_key", "student_id": student.id, **data.model_dump()}
        )
        if previous:
            return {**previous.result, "key": None, "replayed": True}
        member = await db.get(Membership, data.membership_id)
        if not member or member.student_id != student.id:
            fail(404, "membership_not_found")
        team = await db.scalar(select(Team).where(Team.id == member.team_id).with_for_update(key_share=True))
        await db.refresh(member)
        if not member.active or not team or not team.active:
            fail(403, "identity_disabled")
        models = sorted(set(member.models) & set(team.models))
        if not models:
            fail(403, "no_allowed_models")
        keys = (
            await db.scalars(
                select(PersonalKey).where(
                    PersonalKey.membership_id == member.id, PersonalKey.active.is_(True)
                )
            )
        ).all()
        live = [k for k in keys if k.expires_at is None or k.expires_at > now()]
        if live and not data.rotate:
            fail(409, "active_key_exists_use_rotate")
        for key in keys:
            key.active = False
        key, raw = issue(db, request.app, member, models, member.key_expires_at)
        result = {
            "key_id": key.id,
            "prefix": key.prefix,
            "membership_id": member.id,
            "base_url": request.app.state.settings.public_url + "/v1",
        }
        db.add(Operation(id=data.operation_id, fingerprint=fingerprint, result=result))
        audit(
            db,
            request,
            "student_key_issue",
            key.id,
            {"membership_id": member.id, "revoked_count": len(keys)},
            actor="student:" + student.id,
        )
        return {**result, "key": raw, "replayed": False}


@router.post("/logout")
async def logout(request: Request, response: Response):
    async with request.app.state.db() as db, db.begin():
        _, _, session = await identity(db, request, allow_initial=True)
        await db.delete(session)
    response.delete_cookie(COOKIE, path="/student")
    return {"ok": True}
