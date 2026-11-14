"""Remove expired sessions and old raw IPs while preserving accounting and audit history."""

import asyncio
from datetime import timedelta

from sqlalchemy import delete, update

from gateway.config import Settings
from gateway.db import database
from gateway.models import AdminSession, Call, StudentSession, now


async def main():
    settings = Settings.from_env()
    if settings.retention_days < 1:
        raise SystemExit("RETENTION_DAYS must be positive")
    engine, factory = database(settings.database_url)
    async with factory() as db, db.begin():
        sessions = await db.execute(delete(AdminSession).where(AdminSession.expires_at < now()))
        students = await db.execute(delete(StudentSession).where(StudentSession.expires_at < now()))
        ips = await db.execute(
            update(Call)
            .where(Call.started_at < now() - timedelta(days=settings.retention_days), Call.ip.is_not(None))
            .values(ip=None)
        )
    await engine.dispose()
    print(
        f"Removed expired sessions: {sessions.rowcount + students.rowcount}; "
        f"anonymized old request IPs: {ips.rowcount}"
    )


asyncio.run(main())
