import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    student_number: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="student")
    owner: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "students_live_number", "student_number", unique=True, postgresql_where=text("deleted_at IS NULL")
        ),
    )


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    models: Mapped[list] = mapped_column(JSONB, default=list)
    budget: Mapped[int] = mapped_column(BigInteger, default=0)
    period: Mapped[str] = mapped_column(String(8), default="month")
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    rpm: Mapped[int] = mapped_column(Integer, default=60)
    concurrency: Mapped[int] = mapped_column(Integer, default=10)
    max_output: Mapped[int] = mapped_column(Integer, default=2048)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("budget >= 0 AND rpm > 0 AND concurrency > 0 AND max_output > 0"),
        Index("teams_live_code", "code", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    ciphertext: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("team_id", "version"),)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    budget: Mapped[int] = mapped_column(BigInteger, default=0)
    period: Mapped[str] = mapped_column(String(8), default="month")
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    rpm: Mapped[int] = mapped_column(Integer, default=20)
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    models: Mapped[list] = mapped_column(JSONB, default=list)
    key_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "memberships_live_student_team",
            "student_id",
            "team_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("budget >= 0 AND rpm > 0 AND concurrency > 0"),
    )


class PersonalKey(Base):
    __tablename__ = "personal_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    membership_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    models: Mapped[list] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BudgetPeriod(Base):
    __tablename__ = "budget_periods"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(36))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[int] = mapped_column(Integer)
    limit: Mapped[int] = mapped_column(BigInteger)
    spent: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "starts_at"),
        CheckConstraint("reserved >= 0 AND spent >= 0"),
    )


class Price(Base):
    __tablename__ = "prices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    model: Mapped[str] = mapped_column(String(128), index=True)
    upstream_model: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64), unique=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # micro-CNY per million tokens; integer multiply then ceiling once per request.
    input_miss: Mapped[int] = mapped_column(BigInteger)
    input_hit: Mapped[int] = mapped_column(BigInteger)
    output: Mapped[int] = mapped_column(BigInteger)
    estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(500))
    __table_args__ = (
        CheckConstraint("input_miss > 0 AND input_hit >= 0 AND output > 0 AND input_hit <= input_miss"),
    )


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    membership_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    key_id: Mapped[str] = mapped_column(ForeignKey("personal_keys.id"), index=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("credentials.id"))
    price_id: Mapped[str] = mapped_column(ForeignKey("prices.id"))
    member_period_id: Mapped[str] = mapped_column(ForeignKey("budget_periods.id"))
    team_period_id: Mapped[str] = mapped_column(ForeignKey("budget_periods.id"))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    reason: Mapped[str | None] = mapped_column(String(64))
    reservation: Mapped[int] = mapped_column(BigInteger)
    cost: Mapped[int | None] = mapped_column(BigInteger)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    upstream_id: Mapped[str | None] = mapped_column(String(128))
    upstream_status: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    ip: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admission_ms: Mapped[int | None] = mapped_column(Integer)
    first_delta_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        Index("calls_active_team", "team_id", "status"),
        Index("calls_team_started", "team_id", "started_at"),
        Index("calls_member_source", "membership_id", "ip"),
        Index(
            "calls_active_members",
            "team_id",
            "membership_id",
            postgresql_where=text("status IN ('reserved','running','pending')"),
        ),
    )


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    operation: Mapped[str] = mapped_column(String(150), unique=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("budget_periods.id"), index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"))
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    dedup: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(64))
    team_id: Mapped[str | None] = mapped_column(String(36))
    membership_id: Mapped[str | None] = mapped_column(String(36))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StudentAccount(Base):
    __tablename__ = "student_accounts"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StudentSession(Base):
    __tablename__ = "student_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("student_accounts.student_id"), index=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    csrf_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
