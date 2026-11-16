"""Persist rejected attempts, independent call outcomes and event handling history."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "c42e8a910d65"
down_revision = "b7e092dc41af"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calls", sa.Column("outcome_reason", sa.String(64)))
    op.add_column("calls", sa.Column("outcome", sa.String(24), nullable=False, server_default="in_progress"))
    # Older rows did not distinguish delivery from billing. Do not invent success
    # from a 200 response or a manual settlement; preserve the uncertainty.
    op.execute("UPDATE calls SET outcome = 'unknown'")
    op.create_table(
        "rejections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("student_id", sa.String(36), sa.ForeignKey("students.id")),
        sa.Column("membership_id", sa.String(36), sa.ForeignKey("memberships.id")),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id")),
        sa.Column("key_id", sa.String(36), sa.ForeignKey("personal_keys.id")),
        sa.Column("model", sa.String(128)),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("limit_scope", sa.String(24)),
        sa.Column("ip", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rejections_team_id", "rejections", ["team_id"])
    op.create_index("ix_rejections_created_at", "rejections", ["created_at"])
    op.add_column("events", sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"))
    op.add_column(
        "events",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("UPDATE events SET last_seen_at = created_at")
    for name, type_ in (
        ("resolved_at", sa.DateTime(timezone=True)),
        ("resolved_by", sa.String(100)),
        ("resolution", sa.String(500)),
    ):
        op.add_column("events", sa.Column(name, type_))
    op.execute((Path(__file__).parents[1] / "sql/0004_admit.sql").read_text())


def downgrade():
    op.execute((Path(__file__).parents[1] / "sql/0003_admit.sql").read_text())
    for name in ("resolution", "resolved_by", "resolved_at", "last_seen_at", "occurrences"):
        op.drop_column("events", name)
    op.drop_table("rejections")
    op.drop_column("calls", "outcome")
    op.drop_column("calls", "outcome_reason")
