"""Composite indexes for the feed, badge, and dispatcher scans.

The feed reads ``WHERE user_id = ? AND archived_at IS (NOT) NULL ORDER BY
created_at DESC, id DESC`` and the badge counts ``user_id + read_at IS NULL +
archived_at IS NULL`` — both previously leaned on the single-column ``user_id``
index. The dispatcher scans ``status`` plus the stale-claim cutoff on
``claimed_at``. Each composite's leading column covers the single-column index
it replaces, so those are dropped rather than kept as dead weight.

Every create/drop is guarded by an inspector existence check: an adopting host
was *stamped* at the baseline, so its historical chain may have named (or
omitted) these indexes differently — a hardcoded ``DROP INDEX`` would wedge its
boot migration. The guards also make a partially-applied run safely retryable.

On Postgres the indexes build with ``CONCURRENTLY`` (in an autocommit block):
plain ``CREATE INDEX`` takes a SHARE lock that blocks every ``notify()`` — and
the producing domain transaction it rides in — for the duration of the build,
which is not acceptable for a boot-time ``migrate()`` against a live table.
Trade-off: if a concurrent build is interrupted it can leave an INVALID index
behind; drop it manually (``DROP INDEX <name>``) and re-run ``migrate()``.
On SQLite index DDL is cheap and non-concurrent; plain statements are used.

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
    ("notification", "ix_notification_user_archived_created",
     ["user_id", "archived_at", "created_at", "id"]),
    ("notification", "ix_notification_user_read_archived",
     ["user_id", "read_at", "archived_at"]),
    ("notification_delivery", "ix_notification_delivery_status_claimed",
     ["status", "claimed_at"]),
]
# Subsumed by the composites' leading columns.
_OLD = [
    ("notification", "ix_notification_user_id", ["user_id"]),
    ("notification_delivery", "ix_notification_delivery_status", ["status"]),
]


def _existing(table: str) -> set:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _create(table: str, name: str, columns: list) -> None:
    if name in _existing(table):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.create_index(name, table, columns, unique=False, postgresql_concurrently=True)
    else:
        op.create_index(name, table, columns, unique=False)


def _drop(table: str, name: str) -> None:
    if name not in _existing(table):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    for table, name, columns in _NEW:
        _create(table, name, columns)
    for table, name, _ in _OLD:
        _drop(table, name)


def downgrade() -> None:
    for table, name, columns in _OLD:
        _create(table, name, columns)
    for table, name, _ in _NEW:
        _drop(table, name)
