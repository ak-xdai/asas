"""Explicit per-type scope: platform reference data vs org-owned vocabularies.

Adds ``lookup_type.scope`` (issue #35; the no-override model decided in #24).
Existing types backfill to ``platform`` — every library-seeded type is
standards-based reference data, and that matches the enforced behavior since
asas-lookups 0.12.0 (platform rows read-only for orgs) — except legacy
``is_open`` types, which backfill to ``org``: an open list means org users add
values, which only an org-owned type can host, so stamping those ``platform``
would turn every org add into a 403. Hosts declare ``scope='org'`` for their
vocabularies at registration.

**Required host step after this upgrade**: for every org-scoped type the
platform rows become an unserved template, so a host upgrading a database
that already has organizations MUST call ``seed_org_lookups(session, org_id)``
once per existing org (idempotent). Until then those orgs read empty lists —
and a stored template-only code answers 404 — for every org-scoped type,
including legacy open types this revision converts. This migration cannot do
it: org ids live in the host's own tables, which this package never sees.

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

_lookup_type = sa.table(
    "lookup_type",
    sa.column("scope", sa.String()),
    sa.column("is_open", sa.Boolean()),
)


def upgrade() -> None:
    # Adoption-safe and resumable. A host whose own chain (or a create_all
    # against the current models) already carries the column is stamped at
    # the baseline and then runs this revision — but column *presence* is not
    # completed state: the column may exist nullable with NULLs in it. So
    # each step guards itself: add only if absent, always backfill NULLs,
    # tighten to NOT NULL only if still nullable.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"]: c for c in inspector.get_columns("lookup_type")}
    if "scope" not in columns:
        op.add_column("lookup_type", sa.Column("scope", sa.String(), nullable=True))
    op.execute(
        _lookup_type.update()
        .where(_lookup_type.c.scope.is_(None), _lookup_type.c.is_open.is_(True))
        .values(scope="org")
    )
    op.execute(
        _lookup_type.update()
        .where(_lookup_type.c.scope.is_(None))
        .values(scope="platform")
    )
    if columns.get("scope", {"nullable": True})["nullable"]:
        with op.batch_alter_table("lookup_type") as batch_op:
            batch_op.alter_column("scope", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    # Presence-guarded like the upgrade: when the host's own chain carried the
    # column and the upgrade added nothing, there may be nothing to drop (and
    # if there is, the host chain owns it — dropping is still the only honest
    # inverse this revision has; adopt-mode hosts downgrade with their own
    # chain, not this one).
    inspector = sa.inspect(op.get_bind())
    if "scope" not in {c["name"] for c in inspector.get_columns("lookup_type")}:
        return
    with op.batch_alter_table("lookup_type") as batch_op:
        batch_op.drop_column("scope")
