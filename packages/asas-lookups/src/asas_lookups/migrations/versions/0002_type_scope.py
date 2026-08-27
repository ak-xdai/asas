"""Explicit per-type scope: platform reference data vs org-owned vocabularies.

Adds ``lookup_type.scope`` (issue #35; the no-override model decided in #24).
Existing types backfill to ``platform`` — every library-seeded type is
standards-based reference data, and that matches the enforced behavior since
asas-lookups 0.12.0 (platform rows read-only for orgs). Hosts declare
``scope='org'`` for their vocabularies at registration.

Dual-engine rule: plain VARCHAR (no native enum), no server defaults — the
column is added nullable, backfilled, then tightened to NOT NULL via batch
mode so SQLite's table-recreate path works the same as Postgres ALTER.

Revision ID: 0002
Revises: 0001
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Adoption-safe: a host whose own chain (or a create_all against the
    # current models) already carries the column is stamped at the baseline
    # and then runs this revision — skip rather than duplicate the column.
    inspector = sa.inspect(op.get_bind())
    if "scope" in {c["name"] for c in inspector.get_columns("lookup_type")}:
        return
    op.add_column("lookup_type", sa.Column("scope", sa.String(), nullable=True))
    op.execute("UPDATE lookup_type SET scope = 'platform' WHERE scope IS NULL")
    with op.batch_alter_table("lookup_type") as batch_op:
        batch_op.alter_column("scope", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("lookup_type") as batch_op:
        batch_op.drop_column("scope")
