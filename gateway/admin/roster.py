from fastapi import APIRouter, Request
from sqlalchemy import delete, select, text, update

from ..accounts import create_membership, prepare_password, remove_memberships
from ..keys import issue
from ..models import (
    Membership,
    Operation,
    PersonalKey,
    Student,
    StudentAccount,
    StudentSession,
    Team,
    now,
)
from ..operations import audit, operation
from ..schemas import (
    Enroll,
    PasswordReset,
    StudentImport,
)
from ..security import fail

router = APIRouter()


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
        member = await create_membership(
            db, student.id, team.id, data, rpm=data.rpm, concurrency=data.concurrency
        )
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
            prepared[item.student_number] = await prepare_password(
                item.student_number, data.initial_password_mode
            )
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
                member = await create_membership(db, student.id, team.id, data)
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
    temporary, encoded = await prepare_password(student_id, "random")
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
