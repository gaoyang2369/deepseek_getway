"""Administrator-only roster import and verified-identity password reset."""

import secrets

import anyio
from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select, text, update

from .admin import audit, operation, remove_memberships, require_admin
from .models import (
    Membership,
    Operation,
    PersonalKey,
    Student,
    StudentAccount,
    StudentSession,
    Team,
    now,
)
from .schemas import PasswordReset, StudentImport
from .security import fail

router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


@router.get("/students")
async def students(request: Request):
    async with request.app.state.db() as db:
        return [
            {"student_id": s.id, "student_number": s.student_number, "name": s.name, "active": s.active}
            for s in (
                await db.scalars(
                    select(Student)
                    .where(Student.kind == "student", Student.deleted_at.is_(None))
                    .order_by(Student.student_number)
                )
            ).all()
        ]


@router.delete("/students/{student_id}")
async def delete_student(student_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        # Same account-before-team order as student key issuance; serialize with reset.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": student_id})
        student = await db.get(Student, student_id)
        if not student or student.kind != "student":
            fail(404, "student_not_found")
        if student.deleted_at:
            return {"ok": True, "history_retained": True}
        account = await db.scalar(
            select(StudentAccount).where(StudentAccount.student_id == student_id).with_for_update()
        )
        await db.scalars(
            select(Team)
            .where(Team.id.in_(select(Membership.team_id).where(Membership.student_id == student_id)))
            .order_by(Team.id)
            .with_for_update(key_share=True)
        )
        # Identity creation holds this lock after its team lock too.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 1))"), {"id": student.student_number}
        )
        student.active, student.deleted_at = False, now()
        if account:
            account.auth_version += 1
        await db.execute(delete(StudentSession).where(StudentSession.student_id == student_id))
        await remove_memberships(db, Membership.student_id == student_id, student.deleted_at)
        audit(db, request, "student_delete", student_id, {"history_retained": True, "keys_revoked": True})
    return {"ok": True, "history_retained": True}


@router.get("/students/accounts")
async def accounts(request: Request):
    async with request.app.state.db() as db:
        rows = (
            await db.execute(
                select(Student, StudentAccount)
                .join(StudentAccount, StudentAccount.student_id == Student.id)
                .where(Student.deleted_at.is_(None))
                .order_by(Student.student_number)
            )
        ).all()
        return [
            {
                "student_id": s.id,
                "student_number": s.student_number,
                "name": s.name,
                "active": s.active,
                "must_change_password": a.must_change_password,
            }
            for s, a in rows
        ]


@router.post("/students/import", status_code=201)
async def import_students(data: StudentImport, request: Request):
    if request.app.state.settings.production and data.initial_password_mode == "student_number":
        fail(400, "production_requires_random_initial_password")
    if data.expires_at and data.expires_at <= now():
        fail(400, "expiry_in_past")
    # Resolve replay and existing accounts in a short transaction. Hashing is
    # intentionally outside the team lock and outside any database transaction.
    async with request.app.state.db() as db, db.begin():
        previous, _ = await operation(
            db, data.operation_id, {"action": "student_import", **data.model_dump()}
        )
        if previous:
            return {**previous.result, "initial_passwords": [], "replayed": True}
        team = await db.get(Team, data.team_id)
        if not team or not team.active:
            fail(400, "team_unavailable")
        if not set(data.models).issubset(team.models):
            fail(400, "models_outside_team")
        existing = set(
            (
                await db.scalars(
                    select(Student.student_number)
                    .join(StudentAccount, StudentAccount.student_id == Student.id)
                    .where(
                        Student.student_number.in_([item.student_number for item in data.students]),
                        Student.deleted_at.is_(None),
                    )
                )
            ).all()
        )
    prepared = {}
    for item in data.students:
        if item.student_number not in existing:
            initial = (
                item.student_number
                if data.initial_password_mode == "student_number"
                else secrets.token_urlsafe(24)
            )
            encoded = await anyio.to_thread.run_sync(PasswordHasher().hash, initial)
            prepared[item.student_number] = (initial, encoded)
    # Recheck idempotency, permissions and account existence after preparation.
    async with request.app.state.db() as db, db.begin():
        previous, fingerprint = await operation(
            db, data.operation_id, {"action": "student_import", **data.model_dump()}
        )
        if previous:
            return {**previous.result, "initial_passwords": [], "replayed": True}
        team = await db.scalar(select(Team).where(Team.id == data.team_id).with_for_update(key_share=True))
        if not team or not team.active:
            fail(400, "team_unavailable")
        if not set(data.models).issubset(team.models):
            fail(400, "models_outside_team")
        result = {
            "students": [],
            "student_url": request.app.state.settings.public_url + "/student",
            "initial_password_mode": data.initial_password_mode,
        }
        passwords = []
        # Same order as other identity operations; batch re-import never resets a password or budget.
        for item in sorted(data.students, key=lambda s: s.student_number):
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 1))"), {"id": item.student_number}
            )
            student = await db.scalar(
                select(Student).where(
                    Student.student_number == item.student_number, Student.deleted_at.is_(None)
                )
            )
            if student and (
                student.kind != "student"
                or not student.active
                or (item.name is not None and student.name != item.name)
            ):
                fail(409, "student_identity_conflict")
            if not student:
                student = Student(name=item.name or item.student_number, student_number=item.student_number)
                db.add(student)
                await db.flush()
            member = await db.scalar(
                select(Membership).where(
                    Membership.student_id == student.id,
                    Membership.team_id == team.id,
                    Membership.deleted_at.is_(None),
                )
            )
            if not member:
                member = Membership(
                    student_id=student.id,
                    team_id=team.id,
                    budget=data.budget,
                    period=data.period,
                    models=data.models,
                    key_expires_at=data.expires_at,
                )
                db.add(member)
                await db.flush()
            created = await db.get(StudentAccount, student.id) is None
            if created:
                if item.student_number not in prepared:
                    fail(409, "account_changed_retry_import")
                initial, encoded = prepared[item.student_number]
                db.add(StudentAccount(student_id=student.id, password_hash=encoded))
                if data.initial_password_mode == "random":
                    passwords.append({"student_number": item.student_number, "password": initial})
            result["students"].append(
                {
                    "student_id": student.id,
                    "student_number": item.student_number,
                    "membership_id": member.id,
                    "account_created": created,
                }
            )
        db.add(Operation(id=data.operation_id, fingerprint=fingerprint, result=result))
        audit(
            db,
            request,
            "student_roster_import",
            team.id,
            {
                "count": len(data.students),
                "new_accounts": sum(s["account_created"] for s in result["students"]),
                "initial_password_mode": data.initial_password_mode,
            },
        )
        return {**result, "initial_passwords": passwords, "replayed": False}


@router.post("/students/{student_id}/reset-password")
async def reset_password(student_id: str, data: PasswordReset, request: Request):
    temporary = secrets.token_urlsafe(24)
    encoded = await anyio.to_thread.run_sync(PasswordHasher().hash, temporary)
    async with request.app.state.db() as db, db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": student_id})
        student = await db.get(Student, student_id)
        if not student or student.kind != "student" or not student.active:
            fail(404, "student_not_found")
        account = await db.scalar(
            select(StudentAccount).where(StudentAccount.student_id == student_id).with_for_update()
        )
        if account:
            account.password_hash = encoded
            account.must_change_password = True
            account.auth_version += 1
        else:
            db.add(StudentAccount(student_id=student_id, password_hash=encoded))
        await db.execute(delete(StudentSession).where(StudentSession.student_id == student_id))
        # Recovery also invalidates potentially stolen API keys, across all of this student's groups.
        await db.execute(
            update(PersonalKey)
            .where(
                PersonalKey.membership_id.in_(
                    select(Membership.id).where(Membership.student_id == student_id)
                )
            )
            .values(active=False)
        )
        audit(
            db, request, "student_password_reset", student_id, {"reason": data.reason, "keys_revoked": True}
        )
        return {"temporary_password": temporary, "must_change_password": True, "keys_revoked": True}
