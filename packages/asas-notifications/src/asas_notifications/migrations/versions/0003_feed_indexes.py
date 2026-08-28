"""Composite indexes for the feed, badge, and dispatcher scans.

The feed reads ``WHERE user_id = ? [AND org_id = ?] AND archived_at IS (NOT)
NULL ORDER BY created_at DESC, id DESC`` and the badge counts ``user_id
[+ org_id] + read_at IS NULL + archived_at IS NULL`` — both previously leaned
on the single-column ``user_id`` index. ``org_id`` sits second in each
composite so the org-scoped queries (0.15's defense in depth) filter on the
index while unscoped single-tenant queries still use the ``user_id`` prefix.
The dispatcher scans ``status`` plus the stale-claim cutoff on ``claimed_at``.
Each composite's leading column covers the single-column index it replaces, so
those are dropped rather than kept as dead weight. (``org_id`` keeps its own
single-column index: it leads nowhere here, and org-only scans — admin/ops
shapes — don't go through the recipient chokepoint.)

Every create/drop is guarded by an inspector existence check: an adopting host
was *stamped* at the baseline, so its historical chain may have named (or
omitted) these indexes differently — a hardcoded ``DROP INDEX`` would wedge its
boot migration. The guards also make a partially-applied run safely retryable.

On Postgres the indexes build with ``CONCURRENTLY`` (in an autocommit block):
plain ``CREATE INDEX`` takes a SHARE lock that blocks every ``notify()`` — and
the producing domain transaction it rides in — for the duration of the build,
which is not acceptable for a boot-time ``migrate()`` against a live table.
An interrupted concurrent build leaves an INVALID index behind, which the
inspector reports like any other — so on Postgres the existence guard also
checks ``pg_index.indisvalid`` and a name-matching invalid index is dropped
(concurrently) and rebuilt rather than silently kept: without that, a crashed
boot would strand a dead index that never serves a query while this migration
skips ever repairing it. On SQLite index DDL is cheap and non-concurrent;
plain statements are used.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW = [
    # id trails as the ORDER BY tiebreaker (created_at DESC, id DESC).
    ("notification", "ix_notification_user_org_archived_created",
     ["user_id", "org_id", "archived_at", "created_at", "id"]),
    ("notification", "ix_notification_user_org_read_archived",
     ["user_id", "org_id", "read_at", "archived_at"]),
    ("notification_delivery", "ix_notification_delivery_status_claimed",
     ["status", "claimed_at"]),
]
# Subsumed by the composites' leading columns.
_OLD = [
    ("notification", "ix_notification_user_id", ["user_id"]),
    ("notification_delivery", "ix_notification_delivery_status", ["status"]),
]


def _existing(table: str) -> set:
    """Index names the inspector reports on ``table`` right now."""
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _pg_index_valid(name: str) -> bool:
    """Whether the Postgres index ``name`` is valid (``pg_index.indisvalid``).

    The inspector cannot answer this — it reports catalog indexes without
    their validity — so an INVALID leftover from an interrupted CONCURRENTLY
    build has to be read from ``pg_index`` directly. An index the catalog
    doesn't have at all counts as "valid" (there is nothing to repair)."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_catalog.pg_index i "
            "JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :name AND n.nspname = current_schema()"
        ),
        {"name": name},
    ).scalar()
    return True if row is None else bool(row)


def _create(table: str, name: str, columns: list) -> None:
    """Create ``name`` unless a *valid* index of that name already exists.

    On Postgres a name-matching INVALID index (interrupted concurrent build)
    is dropped and rebuilt instead of being skipped."""
    if op.get_bind().dialect.name == "postgresql":
        if name in _existing(table):
            if _pg_index_valid(name):
                return
            with op.get_context().autocommit_block():
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        with op.get_context().autocommit_block():
            op.create_index(name, table, columns, unique=False, postgresql_concurrently=True)
    else:
        if name in _existing(table):
            return
        op.create_index(name, table, columns, unique=False)


def _drop(table: str, name: str) -> None:
    """Drop ``name`` iff it exists (adopting hosts may never have had it)."""
    if name not in _existing(table):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    """Composites in, subsumed single-column indexes out."""
    for table, name, columns in _NEW:
        _create(table, name, columns)
    for table, name, _ in _OLD:
        _drop(table, name)


def downgrade() -> None:
    """Restore the single-column indexes, drop the composites."""
    for table, name, columns in _OLD:
        _create(table, name, columns)
    for table, name, _ in _NEW:
        _drop(table, name)
