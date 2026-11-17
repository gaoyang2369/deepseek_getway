"""Check settled spending at admission; release outstanding legacy holds."""

from pathlib import Path

from alembic import op

revision = "d58b6f2a901e"
down_revision = "c42e8a910d65"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve old reserve/settle entries and actual charges. Record the policy
    # change as explicit releases rather than deleting financial history.
    op.execute("""
        INSERT INTO ledger(id,operation,period_id,kind,amount,reason,created_at)
        SELECT gen_random_uuid()::text,'release-policy:'||id,id,'reserve_release',
               -reserved,'Switch to settled-spend budget checks',clock_timestamp()
        FROM budget_periods WHERE reserved > 0
    """)
    op.execute("UPDATE budget_periods SET reserved=0 WHERE reserved <> 0")
    op.execute("UPDATE calls SET reservation=0 WHERE status <> 'settled' AND reservation <> 0")
    op.execute((Path(__file__).parents[1] / "sql/0005_admit.sql").read_text())


def downgrade():
    # Releases are historical facts. Do not resurrect old holds; older settle
    # code can safely reconcile these zero-reservation calls.
    op.execute((Path(__file__).parents[1] / "sql/0004_admit.sql").read_text())
