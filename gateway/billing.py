import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from .models import (
    Audit,
    BudgetPeriod,
    Call,
    Event,
    Ledger,
    Membership,
    PersonalKey,
    Price,
    Student,
    Team,
    now,
    uid,
)
from .security import digest, fail

ACTIVE = ("reserved", "running", "pending")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def period_bounds(at: datetime, period: str):
    local = at.astimezone(SHANGHAI)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif period == "day":
        end = start + timedelta(days=1)
    else:
        raise ValueError("unsupported period")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def fee(prompt: int, completion: int, hit: int, price: Price) -> int:
    numerator = (prompt - hit) * price.input_miss + hit * price.input_hit + completion * price.output
    return (numerator + 999999) // 1000000


def parse_usage(data: dict | None):
    if not isinstance(data, dict):
        return None
    prompt, completion = data.get("prompt_tokens"), data.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
        return None
    details = data.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return None
    hit = data.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0))
    miss = data.get("prompt_cache_miss_tokens", prompt - hit if type(hit) is int else -1)
    if type(hit) is not int or type(miss) is not int or hit < 0 or miss < 0 or hit + miss != prompt:
        return None
    # Store token counters only. Never persist arbitrary provider fields as they may contain text.
    result = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }
    for name in ("prompt_tokens_details", "completion_tokens_details"):
        value = data.get(name)
        if isinstance(value, dict):
            result[name] = {
                k: v for k, v in value.items() if k.endswith("tokens") and type(v) is int and v >= 0
            }
    return result


async def event(db, kind, team_id=None, member_id=None, detail=None, scope=None):
    # One event per subject and hour, or a supplied persistent budget period.
    bucket = scope or now().strftime("%Y%m%d%H")
    dedup = hashlib.sha256(f"{kind}:{team_id}:{member_id}:{bucket}".encode()).hexdigest()
    await db.execute(
        insert(Event)
        .values(
            id=uid(),
            dedup=dedup,
            kind=kind,
            team_id=team_id,
            membership_id=member_id,
            detail=detail or {},
            resolved=False,
            created_at=now(),
        )
        .on_conflict_do_nothing(index_elements=["dedup"])
    )


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


RATE_LUA = """
local a = redis.call('INCR', KEYS[1])
if a == 1 then redis.call('EXPIRE', KEYS[1], 60) end
local b = redis.call('INCR', KEYS[2])
if b == 1 then redis.call('EXPIRE', KEYS[2], 60) end
return {a, b}
"""


async def get_period(db, kind, owner, at):
    start, end = period_bounds(at, owner.period)
    await db.execute(
        insert(BudgetPeriod)
        .values(
            id=uid(),
            owner_type=kind,
            owner_id=owner.id,
            starts_at=start,
            ends_at=end,
            policy_version=owner.policy_version,
            limit=owner.budget,
            spent=0,
            reserved=0,
        )
        .on_conflict_do_nothing(index_elements=["owner_type", "owner_id", "starts_at"])
    )
    return await db.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.owner_type == kind,
            BudgetPeriod.owner_id == owner.id,
            BudgetPeriod.starts_at == start,
        )
    )


async def admit(app, authorization, body, request_id, ip, started):
    factory, settings = app.state.db, app.state.settings
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 256:
        fail(401, "invalid_api_key")
    key_digest = digest(authorization[7:], settings.hmac_key)
    route = app.state.admission.route(key_digest)
    if route is None:
        async with factory() as db:
            _, member, _, team = await authenticate(db, authorization, settings)
        route = team.id, member.id
        app.state.admission.remember_route(key_digest, *route)
    team_id, member_id = route
    # No lock or DB connection is held during Redis I/O. Limits and permissions
    # are rechecked with current database values inside the atomic admission.
    rates = await app.state.redis.eval(
        RATE_LUA, 2, f"rate:{{{team_id}}}:team", f"rate:{{{team_id}}}:{member_id}"
    )
    # Include headroom for server-added/replaced max_tokens. No prompt goes to SQL.
    input_bound = (
        len(json.dumps(body, ensure_ascii=False).encode()) + 64 * len(body["messages"]) + 256 + 64
    ) * 2
    result = await app.state.admission.submit(
        team_id,
        {
            "key_digest": key_digest,
            "team_attempts": rates[0],
            "member_attempts": rates[1],
            "model": body["model"],
            "output": body.get("max_tokens"),
            "input_bound": input_bound,
            "request_id": request_id,
            "ip": ip,
        },
    )
    if "error" in result:
        if result["status"] in (401, 403):
            app.state.admission.routes.pop(key_digest, None)
        fail(result["status"], result["error"])
    # The transaction has committed before this timer or any upstream dispatch.
    values = result["call"]
    for name in ("started_at", "ended_at"):
        if values.get(name):
            values[name] = datetime.fromisoformat(values[name])
    call = Call(**values)
    call.admission_ms = round((time.perf_counter() - started) * 1000)
    body["max_tokens"] = result["max_tokens"]
    return call, result["ciphertext"], result["upstream_model"]


async def settle(
    factory,
    request_id,
    usage=None,
    reason=None,
    upstream_status=None,
    upstream_id=None,
    first_delta_ms=None,
    admission_ms=None,
    total_ms=None,
    manual_cost=None,
    manual_audit=None,
    thresholds=(80, 100),
):
    async with factory() as db, db.begin():
        call = await db.scalar(select(Call).where(Call.id == request_id).with_for_update())
        if call.status == "settled":
            return False
        normalized = parse_usage(usage)
        call.ended_at = now()
        if upstream_status is not None:
            call.upstream_status = upstream_status
        if isinstance(upstream_id, str):
            call.upstream_id = upstream_id[:128]
        if admission_ms is not None:
            call.admission_ms = admission_ms
        if first_delta_ms is not None:
            call.first_delta_ms = first_delta_ms
        if total_ms is not None:
            call.total_ms = total_ms
        if normalized is None and manual_cost is None:
            call.status = "pending"
            call.reason = reason or "missing_usage"
            await event(
                db,
                "pending_usage",
                call.team_id,
                call.membership_id,
                {"request_id": call.id, "reason": call.reason},
                scope=call.id,
            )
            return False
        price = await db.get(Price, call.price_id)
        actual = (
            manual_cost
            if manual_cost is not None
            else fee(
                normalized["prompt_tokens"],
                normalized["completion_tokens"],
                normalized["prompt_cache_hit_tokens"],
                price,
            )
        )
        periods = (
            await db.scalars(
                select(BudgetPeriod)
                .where(BudgetPeriod.id.in_([call.member_period_id, call.team_period_id]))
                .order_by(BudgetPeriod.owner_type.desc(), BudgetPeriod.id)
                .with_for_update()
            )
        ).all()
        for p in periods:
            p.reserved -= call.reservation
            p.spent += actual
            db.add(
                Ledger(
                    operation=f"settle:{call.id}:{p.id}",
                    period_id=p.id,
                    call_id=call.id,
                    kind="settle",
                    amount=actual,
                    reason=manual_audit["detail"]["reason"] if manual_audit else reason or "provider_usage",
                )
            )
            for threshold in thresholds:
                if p.limit and (p.spent + p.reserved) * 100 >= p.limit * threshold:
                    await event(
                        db,
                        f"budget_{threshold}",
                        call.team_id,
                        call.membership_id if p.owner_type == "membership" else None,
                        {"period_id": p.id, "spent": p.spent, "reserved": p.reserved, "limit": p.limit},
                        scope=p.id,
                    )
        if actual > call.reservation:
            await event(
                db,
                "reservation_exceeded",
                call.team_id,
                call.membership_id,
                {"request_id": call.id, "reservation": call.reservation, "cost": actual},
                scope=call.id,
            )
        call.cost, call.usage, call.status, call.reason = actual, normalized, "settled", reason
        if manual_audit:
            db.add(Audit(**manual_audit))
        return True


async def recover(factory):
    # Called under a session-level singleton advisory lock at process startup.
    # A previous process may have sent any reserved request: never refund by TTL.
    async with factory() as db, db.begin():
        await db.execute(
            update(Call)
            .where(Call.status.in_(("reserved", "running")))
            .values(status="pending", reason="process_interrupted", ended_at=now())
        )
        count = await db.scalar(select(func.count()).select_from(Call).where(Call.status == "pending"))
        if count:
            await event(db, "recovery_pending", detail={"count": count})
