import secrets
from datetime import timedelta

from fastapi import APIRouter, Request, Response

from ..accounts import password_matches
from ..models import (
    AdminSession,
    now,
)
from ..operations import audit
from ..schemas import (
    Login,
)
from ..security import check_origin, client_ip, digest, fail


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
            check_origin(request)
            supplied = digest(request.headers.get("x-csrf-token", ""), settings.hmac_key)
            if not secrets.compare_digest(supplied, session.csrf_digest):
                fail(403, "csrf_required")


auth_router = APIRouter(prefix="/admin/api")
router = APIRouter()


@auth_router.post("/login")
async def login(data: Login, request: Request, response: Response):
    settings = request.app.state.settings
    check_origin(request)
    ip_hash = digest(client_ip(request, settings), settings.hmac_key)
    count = await request.app.state.redis.eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n",
        1,
        f"login:{ip_hash}",
    )
    if count > 10:
        fail(429, "login_rate_limit")

    valid = await password_matches(settings.admin_hash, data.password)
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
