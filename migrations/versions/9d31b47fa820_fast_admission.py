"""Bound admission queries and move the locked transaction into PostgreSQL."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "9d31b47fa820"
down_revision = "7ac260319e01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("calls_team_started", "calls", ["team_id", "started_at"])
    op.create_index("calls_member_source", "calls", ["membership_id", "ip"])
    op.create_index(
        "calls_active_members",
        "calls",
        ["team_id", "membership_id"],
        postgresql_where=sa.text("status IN ('reserved','running','pending')"),
    )
    op.execute((Path(__file__).parents[1] / "sql/0003_admit.sql").read_text())


def downgrade():
    op.execute("DROP FUNCTION gateway_admit_batch(jsonb)")
    op.execute("DROP FUNCTION gateway_admit(text,text,integer,bigint,text,text,bigint,bigint,timestamptz)")
    op.drop_index("calls_active_members", table_name="calls")
    op.drop_index("calls_member_source", table_name="calls")
    op.drop_index("calls_team_started", table_name="calls")
