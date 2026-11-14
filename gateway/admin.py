import csv
import hashlib
import io
import json
import secrets
from datetime import datetime, timedelta

import anyio
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import case, delete, func, or_, select, text, update

from .accounts import prepare_password
from .billing import get_period, settle
from .models import (
    AdminSession,
    Audit,
    BudgetPeriod,
    Call,
    Credential,
    Event,
    Ledger,
    Membership,
    Operation,
    PersonalKey,
    Price,
    Student,
    StudentAccount,
    Team,
    now,
    uid,
)
from .official_prices import SOURCE, PricingFormatError, fetch_prices
from .schemas import (
    BudgetUpdate,
    CredentialRotate,
    Enroll,
    KeyIssue,
    Login,
    Reconcile,
    Strict,
    TeamCreate,
    TeamPatch,
    TestUsageClear,
)
from .security import client_ip, digest, fail, new_key


def audit(db, request, action, target, detail=None):
    db.add(
        Audit(
            actor=request.app.state.settings.admin_name,
            action=action,
            target=target,
            detail=detail or {},
            request_id=request.state.request_id,
        )
    )


async def require_admin(request: Request):
    settings = request.app.state.settings
    cookie = request.cookies.get("lab_session", "")
    if len(cookie) > 200 or not cookie:
        fail(401, "admin_login_required")
    async with request.app.state.db() as db:
        session = await db.get(AdminSession, digest(cookie, settings.hmac_key))
        if not session or session.expires_at <= now():
            fail(401, "admin_login_required")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and origin != settings.public_url:
                fail(403, "invalid_origin")
            supplied = digest(request.headers.get("x-csrf-token", ""), settings.hmac_key)
            if not secrets.compare_digest(supplied, session.csrf_digest):
                fail(403, "csrf_required")


auth_router = APIRouter(prefix="/admin/api")
router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


@auth_router.post("/login")
async def login(data: Login, request: Request, response: Response):
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin and origin != settings.public_url:
        fail(403, "invalid_origin")
    ip_hash = digest(client_ip(request, settings), settings.hmac_key)
    count = await request.app.state.redis.eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n",
        1,
        f"login:{ip_hash}",
    )
    if count > 10:
        fail(429, "login_rate_limit")

    def check():
        try:
            return PasswordHasher().verify(settings.admin_hash, data.password)
        except (VerificationError, InvalidHashError):
            return False

    valid = await anyio.to_thread.run_sync(check)
    if not valid or not secrets.compare_digest(data.username, settings.admin_name):
        fail(401, "invalid_login")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    async with request.app.state.db() as db, db.begin():
        db.add(
            AdminSession(
                digest=digest(token, settings.hmac_key),
                csrf_digest=digest(csrf, settings.hmac_key),
                expires_at=now() + timedelta(seconds=settings.session_seconds),
            )
        )
        audit(db, request, "login", "admin")
    response.set_cookie(
        "lab_session",
        token,
        httponly=True,
        secure=settings.production,
        samesite="strict",
        max_age=settings.session_seconds,
        path="/admin",
    )
    return {"csrf_token": csrf, "expires_in": settings.session_seconds}


@router.post("/logout")
async def logout(request: Request, response: Response):
    async with request.app.state.db() as db, db.begin():
        session = await db.get(
            AdminSession, digest(request.cookies["lab_session"], request.app.state.settings.hmac_key)
        )
        await db.delete(session)
        audit(db, request, "logout", "admin")
    response.delete_cookie("lab_session", path="/admin")
    return {"ok": True}


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
        for k, v in changes.items():
            setattr(t, k, v)
        audit(db, request, "team_update", t.id, changes)
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


async def remove_memberships(db, condition, at):
    """Caller holds the team mutex; preserve every record used for accounting."""
    member_ids = select(Membership.id).where(condition)
    await db.execute(
        update(PersonalKey).where(PersonalKey.membership_id.in_(member_ids)).values(active=False)
    )
    await db.execute(
        update(Membership)
        .where(condition, Membership.deleted_at.is_(None))
        .values(active=False, deleted_at=at)
    )


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


async def operation(db, operation_id, data):
    # Includes the action context; reused IDs with different payloads must fail.
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"), {"id": operation_id})
    previous = await db.get(Operation, operation_id)
    if previous and previous.fingerprint != fingerprint:
        fail(409, "idempotency_conflict")
    return previous, fingerprint


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


@router.post("/enroll", status_code=201)
async def enroll(data: Enroll, request: Request):
    if data.expires_at and data.expires_at <= now():
        fail(400, "expiry_in_past")
    mode = data.initial_password_mode or (
        "random" if request.app.state.settings.production else "student_number"
    )
    if data.kind == "student" and request.app.state.settings.production and mode == "student_number":
        fail(400, "production_requires_random_initial_password")
    initial, encoded = (
        await prepare_password(data.student_number, mode) if data.kind == "student" else (None, None)
    )
    async with request.app.state.db() as db, db.begin():
        previous, fingerprint = await operation(
            db, data.operation_id, {"action": "enroll", **data.model_dump()}
        )
        if previous:
            return {**previous.result, "replayed": True, "key": None, "temporary_password": None}
        team = await db.scalar(select(Team).where(Team.id == data.team_id).with_for_update(key_share=True))
        if not team or not team.active:
            fail(400, "team_unavailable")
        if not set(data.models).issubset(team.models):
            fail(400, "models_outside_team")
        if data.kind == "service" and not data.owner:
            fail(400, "service_owner_required")
        # Cross-team creation of the same student is serialized on the unique business identity.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 1))"), {"id": data.student_number}
        )
        student = await db.scalar(
            select(Student).where(Student.student_number == data.student_number, Student.deleted_at.is_(None))
        )
        if student and (
            not student.active
            or student.name != data.name
            or student.kind != data.kind
            or student.owner != data.owner
        ):
            fail(409, "student_identity_conflict")
        if not student:
            student = Student(
                name=data.name, student_number=data.student_number, kind=data.kind, owner=data.owner
            )
            db.add(student)
            await db.flush()
        existing = await db.scalar(
            select(Membership).where(
                Membership.student_id == student.id,
                Membership.team_id == team.id,
                Membership.deleted_at.is_(None),
            )
        )
        if existing:
            fail(409, "membership_exists_use_key_issue")
        member = Membership(
            student_id=student.id,
            team_id=team.id,
            budget=data.budget,
            period=data.period,
            models=data.models,
            rpm=data.rpm,
            concurrency=data.concurrency,
            key_expires_at=data.expires_at,
        )
        db.add(member)
        await db.flush()
        account_created = False
        key, raw = None, None
        if data.kind == "student":
            if await db.get(StudentAccount, student.id) is None:
                db.add(StudentAccount(student_id=student.id, password_hash=encoded))
                account_created = True
        else:
            key, raw = issue(db, request.app, member, data.models, data.expires_at)
        result = {
            "student_id": student.id,
            "membership_id": member.id,
            "key_id": key.id if key else None,
            "prefix": key.prefix if key else None,
            "base_url": request.app.state.settings.public_url + "/v1",
            "student_url": request.app.state.settings.public_url + "/student"
            if data.kind == "student"
            else None,
            "account_created": account_created,
            "initial_password_mode": mode if data.kind == "student" else None,
        }
        db.add(Operation(id=data.operation_id, fingerprint=fingerprint, result=result))
        audit(
            db,
            request,
            "enroll",
            member.id,
            {"student_id": student.id, "team_id": team.id, "budget": data.budget},
        )
        return {
            **result,
            "key": raw,
            "replayed": False,
            "temporary_password": initial if account_created and mode == "random" else None,
        }


@router.get("/memberships")
async def memberships(request: Request, include_deleted: bool = False):
    async with request.app.state.db() as db:
        query = (
            select(Membership, Student, Team)
            .join(Student, Student.id == Membership.student_id)
            .join(Team, Team.id == Membership.team_id)
            .order_by(Student.student_number)
        )
        if not include_deleted:
            query = query.where(
                Membership.deleted_at.is_(None), Student.deleted_at.is_(None), Team.deleted_at.is_(None)
            )
        rows = (await db.execute(query)).all()
        return [
            {
                "id": m.id,
                "student_id": s.id,
                "name": s.name,
                "student_number": s.student_number,
                "team_id": t.id,
                "team": t.name,
                "active": m.active,
                "budget": m.budget,
                "period": m.period,
                "models": m.models,
                "kind": s.kind,
                "owner": s.owner,
                "key_expires_at": m.key_expires_at,
                "deleted_at": m.deleted_at or s.deleted_at or t.deleted_at,
            }
            for m, s, t in rows
        ]


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
async def keys(request: Request, include_deleted: bool = False):
    async with request.app.state.db() as db:
        query = (
            select(PersonalKey)
            .join(Membership)
            .join(Student)
            .join(Team, Team.id == Membership.team_id)
            .order_by(PersonalKey.created_at.desc())
        )
        if not include_deleted:
            query = query.where(
                Membership.deleted_at.is_(None), Student.deleted_at.is_(None), Team.deleted_at.is_(None)
            )
        return [
            {
                k: getattr(row, k)
                for k in ("id", "membership_id", "prefix", "models", "active", "expires_at", "created_at")
            }
            for row in (await db.scalars(query)).all()
        ]


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
            "available": p.limit - p.spent - p.reserved,
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
                "available": p.limit - p.spent - p.reserved,
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
            counts = {
                "calls": len(calls),
                "budget_periods": removed_periods.rowcount,
                "ledger_entries": ledgers.rowcount,
            }
            audit(db, request, "test_usage_clear", team_id, {"name": team.name, **counts})
    return {"ok": True, "removed": counts, "configuration_preserved": True}


class PriceCreate(Strict):
    model: str
    upstream_model: str
    version: str
    input_miss: int
    input_hit: int
    output: int
    estimated: bool = True
    note: str


@router.post("/prices", status_code=201)
async def create_price(data: PriceCreate, request: Request):
    if (
        any(not x or len(x) > 128 for x in (data.model, data.upstream_model, data.version))
        or len(data.version) > 64
        or not 1 <= len(data.note) <= 500
    ):
        fail(400, "invalid_price_metadata")
    if (
        not 0 <= data.input_hit <= data.input_miss
        or min(data.input_miss, data.output) <= 0
        or max(data.input_miss, data.output) > 10**15
    ):
        fail(400, "invalid_price")
    async with request.app.state.db() as db, db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": data.model})
        at = now()
        for old in (
            await db.scalars(select(Price).where(Price.model == data.model, Price.ends_at.is_(None)))
        ).all():
            old.ends_at = at
        p = Price(**data.model_dump(), starts_at=at)
        db.add(p)
        await db.flush()
        audit(db, request, "price_create", p.id, data.model_dump())
        return {"id": p.id, "version": p.version}


@router.post("/prices/import-official")
async def import_official_prices(request: Request):
    try:
        tariffs = await fetch_prices()
    except (httpx.HTTPError, UnicodeError):
        fail(502, "official_pricing_unavailable")
    except PricingFormatError:
        fail(502, "official_pricing_format_changed")
    created, unchanged = [], []
    async with request.app.state.db() as db, db.begin():
        # Match manual price publication's lock and lock all models in stable order.
        for tariff in sorted(tariffs, key=lambda item: item["model"]):
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": tariff["model"]}
            )
        at = now()
        for tariff in tariffs:
            model, rates = tariff["model"], tariff["peak"]
            off_peak_note = "空闲价（元/百万 Token，未命中/命中/输出）：" + "/".join(
                f"{tariff['off_peak'][key] / 1_000_000:g}" for key in ("input_miss", "input_hit", "output")
            )
            existing = list(
                (await db.scalars(select(Price).where(Price.model == model, Price.ends_at.is_(None)))).all()
            )
            if (
                len(existing) == 1
                and existing[0].upstream_model == model
                and existing[0].estimated
                and existing[0].note.startswith("DeepSeek 官网自动导入")
                and all(getattr(existing[0], key) == value for key, value in rates.items())
                and off_peak_note in existing[0].note
            ):
                unchanged.append(model)
                continue
            for old in existing:
                old.ends_at = at
            note = (
                f"DeepSeek 官网自动导入；核对日期 {at.date()}；人民币/百万 Token。"
                "按官方高峰价保守估算，不按时段或法定节假日切换，实际账单可能较低。"
                f"{off_peak_note}。来源：{SOURCE}"
            )
            row = Price(
                model=model,
                upstream_model=model,
                version="official-" + at.strftime("%Y%m%d") + "-" + uid(),
                starts_at=at,
                estimated=True,
                note=note,
                **rates,
            )
            db.add(row)
            await db.flush()
            audit(db, request, "price_import_official", row.id, {"model": model, "source": SOURCE, **rates})
            created.append(model)
    return {
        "created": created,
        "unchanged": unchanged,
        "source": SOURCE,
        "fetched_at": at,
        "policy": "official_peak_estimate",
        "tariffs": tariffs,
    }


@router.delete("/prices/{price_id}")
async def remove_price(price_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        row = await db.get(Price, price_id)
        if row is None:
            fail(404, "price_not_found")
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": row.model})
        await db.refresh(row)
        at = now()
        if row.ends_at is None or row.ends_at > at:
            row.ends_at = at
            audit(db, request, "price_remove", price_id, {"model": row.model, "version": row.version})
    return {"ok": True, "history_preserved": True}


@router.get("/prices")
async def prices(request: Request, include_ended: bool = False):
    async with request.app.state.db() as db:
        query = select(Price).order_by(Price.starts_at.desc())
        if not include_ended:
            query = query.where(Price.starts_at <= now(), or_(Price.ends_at.is_(None), Price.ends_at > now()))
        return [
            {
                k: getattr(p, k)
                for k in (
                    "id",
                    "model",
                    "upstream_model",
                    "version",
                    "input_miss",
                    "input_hit",
                    "output",
                    "estimated",
                    "note",
                    "starts_at",
                    "ends_at",
                )
            }
            for p in (await db.scalars(query)).all()
        ]


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
                    from .billing import SHANGHAI

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
