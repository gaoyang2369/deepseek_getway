"""Student accounts and separate sessions; preserve all existing budgets and keys."""

import sqlalchemy as sa
from alembic import op

revision = "7ac260319e01"
down_revision = "0246bbb4a349"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("memberships", sa.Column("key_expires_at", sa.DateTime(timezone=True)))
    op.execute("""UPDATE memberships SET key_expires_at = limits.deadline FROM (
        SELECT membership_id, CASE WHEN bool_or(expires_at IS NULL) THEN NULL
        ELSE max(expires_at) END AS deadline FROM personal_keys GROUP BY membership_id
    ) limits WHERE memberships.id = limits.membership_id""")
    op.create_table(
        "student_accounts",
        sa.Column("student_id", sa.String(36), sa.ForeignKey("students.id"), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "student_sessions",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("student_id", sa.String(36), sa.ForeignKey("student_accounts.student_id"), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_student_sessions_student_id", "student_sessions", ["student_id"])


def downgrade():
    op.drop_table("student_sessions")
    op.drop_table("student_accounts")
    op.drop_column("memberships", "key_expires_at")
