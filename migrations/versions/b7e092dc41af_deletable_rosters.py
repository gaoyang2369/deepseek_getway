"""Remove roster entries without removing historical calls, budgets or credentials."""

import sqlalchemy as sa
from alembic import op

revision = "b7e092dc41af"
down_revision = "9d31b47fa820"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("teams", "students", "memberships"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    for table, columns, index in (
        ("teams", ["code"], "teams_live_code"),
        ("students", ["student_number"], "students_live_number"),
        ("memberships", ["student_id", "team_id"], "memberships_live_student_team"),
    ):
        op.create_index(index, table, columns, unique=True, postgresql_where=sa.text("deleted_at IS NULL"))
        # Resolve the original constraint name rather than assuming a naming convention.
        constraint = next(
            c["name"]
            for c in sa.inspect(op.get_bind()).get_unique_constraints(table)
            if c["column_names"] == columns
        )
        op.drop_constraint(constraint, table, type_="unique")


def downgrade():
    # PostgreSQL rejects this downgrade if deleted identifiers have been reused.
    # Never discard or rename records to make the old constraints fit.
    for table, columns, index in (
        ("teams", ["code"], "teams_live_code"),
        ("students", ["student_number"], "students_live_number"),
        ("memberships", ["student_id", "team_id"], "memberships_live_student_team"),
    ):
        op.create_unique_constraint(None, table, columns)
        op.drop_index(index, table_name=table)
        op.drop_column(table, "deleted_at")
