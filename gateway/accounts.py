"""Provision student logins without issuing or exposing personal API keys."""

import secrets

import anyio
from argon2 import PasswordHasher
from sqlalchemy import select, text

from .models import Student, StudentAccount


async def prepare_password(number, mode):
    initial = number if mode == "student_number" else secrets.token_urlsafe(24)
    encoded = await anyio.to_thread.run_sync(PasswordHasher().hash, initial)
    return initial, encoded


async def backfill_demo_accounts(factory, settings):
    # Existing demo students can use the documented demo initial password.
    # Production initial credentials are generated in the explicit enrollment flow.
    if settings.production:
        return 0
    async with factory() as db:
        students = (
            await db.scalars(
                select(Student)
                .outerjoin(StudentAccount)
                .where(
                    Student.kind == "student",
                    Student.active.is_(True),
                    Student.deleted_at.is_(None),
                    StudentAccount.student_id.is_(None),
                )
            )
        ).all()
    created = 0
    for student in students:
        _, encoded = await prepare_password(student.student_number, "student_number")
        async with factory() as db, db.begin():
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": student.id}
            )
            current = await db.get(Student, student.id)
            if (
                current
                and current.active
                and not current.deleted_at
                and not await db.get(StudentAccount, current.id)
            ):
                db.add(StudentAccount(student_id=current.id, password_hash=encoded))
                created += 1
    return created
