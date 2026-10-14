"""asas-audit baseline: the append-only, hash-chained audit log.

Append-only is enforced three ways, and each layer covers a case the others do
not:

1. no mutating code in the package,
2. row-level security, so one tenant cannot reach another's history,
3. **a trigger that rejects UPDATE and DELETE for anyone**, which is the only
   layer that survives a compromised application or somebody at a psql prompt.

The trigger is the reason this table is different from every other one in the
family. It is also why the downgrade has to drop it before dropping the table:
a trigger function outlives its table otherwise.

Row-level security is applied through ``asas_tenancy`` rather than spelled out
here, so this package and the host's own tables are protected by one definition
instead of two that can drift. On SQLite both the policy and the trigger are
skipped: that dialect has neither, and it serialises writers anyway, so the chain
is safe there by the database's own semantics rather than by ours.

Revision ID: 0001
Revises:
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import asas_tenancy

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOCK_MUTATION = """
CREATE FUNCTION audit_event_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only; % is forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "audit_event",
        # The chain order, assigned by the database. Verification walks rows in
        # this order, so it cannot come from the application.
        #
        # `with_variant` is load-bearing: SQLite auto-increments a plain
        # `INTEGER PRIMARY KEY` and nothing else, so a `BIGINT` primary key is an
        # ordinary column there and every insert fails its NOT NULL. Postgres
        # keeps the bigint, which is what an append-only table wants.
        sa.Column(
            "seq",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        # Opaque string identity throughout: an integer column would force a
        # UUID-keyed host to fork the package. See models.py.
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hash_prev", sa.LargeBinary(32), nullable=True),
        sa.Column("hash_current", sa.LargeBinary(32), nullable=False),
    )
    op.create_index("ix_audit_event_id", "audit_event", ["id"], unique=True)
    op.create_index("ix_audit_event_org_id", "audit_event", ["org_id"])
    # The tail read on every append. Raw SQL for the DESC, which Alembic's
    # create_index cannot express and which is the whole reason for the index:
    # without it the newest-row-per-tenant lookup sorts.
    op.execute("CREATE INDEX ix_audit_event_org_seq ON audit_event (org_id, seq DESC)")
    op.create_index(
        "ix_audit_event_resource",
        "audit_event",
        ["org_id", "resource_type", "resource_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_event_actor", "audit_event", ["org_id", "actor", "occurred_at"]
    )

    # One definition of the policy, shared with the host's own tables.
    asas_tenancy.enable_rls("audit_event", column="org_id", column_type="character varying")

    if op.get_bind().dialect.name == "postgresql":
        op.execute(_BLOCK_MUTATION)
        op.execute(
            "CREATE TRIGGER audit_event_no_mutation"
            " BEFORE UPDATE OR DELETE ON audit_event"
            " FOR EACH ROW EXECUTE FUNCTION audit_event_block_mutation()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_event_no_mutation ON audit_event")
        op.execute("DROP FUNCTION IF EXISTS audit_event_block_mutation()")
    asas_tenancy.disable_rls("audit_event")
    op.drop_index("ix_audit_event_actor", table_name="audit_event")
    op.drop_index("ix_audit_event_resource", table_name="audit_event")
    op.execute("DROP INDEX IF EXISTS ix_audit_event_org_seq")
    op.drop_index("ix_audit_event_org_id", table_name="audit_event")
    op.drop_index("ix_audit_event_id", table_name="audit_event")
    op.drop_table("audit_event")
