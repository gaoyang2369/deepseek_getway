"""Provision student logins without issuing or exposing personal API keys."""

import secrets

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select, text, update

from .models import Membership, PersonalKey, Student, StudentAccount


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


async def password_matches(encoded, password):
    def verify():
        try:
            return PasswordHasher().verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False

    return await anyio.to_thread.run_sync(verify)


async def create_membership(db, student_id, team_id, data, **limits):
    member = Membership(
        student_id=student_id,
        team_id=team_id,
        budget=data.budget,
        period=data.period,
        models=data.models,
        key_expires_at=data.expires_at,
        **limits,
    )
    db.add(member)
    await db.flush()
    return member
