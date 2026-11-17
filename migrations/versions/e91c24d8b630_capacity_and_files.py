"""Optional capacity limits, scoped image uploads and rejection diagnostics."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e91c24d8b630"
down_revision = "d58b6f2a901e"
branch_labels = None
depends_on = None


def replace_checks(zero_allowed):
    comparator = ">=" if zero_allowed else ">"
    for table in ("teams", "memberships"):
        for check in sa.inspect(op.get_bind()).get_check_constraints(table):
            if "concurrency" in check["sqltext"]:
                op.drop_constraint(check["name"], table, type_="check")
        condition = f"budget >= 0 AND rpm {comparator} 0 AND concurrency {comparator} 0"
        if table == "teams":
            condition += " AND max_output > 0"
        op.create_check_constraint(f"{table}_budget_capacity", table, condition)


def upgrade():
    replace_checks(True)
    # Remove only the formerly implicit defaults; preserve explicit custom limits.
    op.execute("UPDATE memberships SET rpm=0 WHERE rpm=20")
    op.execute("UPDATE memberships SET concurrency=0 WHERE concurrency=2")
    op.add_column("rejections", sa.Column("details", postgresql.JSONB(), nullable=True))
    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("membership_id", sa.String(36), sa.ForeignKey("memberships.id"), nullable=False),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("upstream_id", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_uploaded_files_membership_id", "uploaded_files", ["membership_id"])
    op.execute((Path(__file__).parents[1] / "sql/0006_admit.sql").read_text())


def downgrade():
    op.execute((Path(__file__).parents[1] / "sql/0005_admit.sql").read_text())
    op.drop_table("uploaded_files")
    op.drop_column("rejections", "details")
    for table, rpm, concurrency in (("teams", 60, 10), ("memberships", 20, 2)):
        op.execute(f"UPDATE {table} SET rpm={rpm} WHERE rpm=0")
        op.execute(f"UPDATE {table} SET concurrency={concurrency} WHERE concurrency=0")
    replace_checks(False)
