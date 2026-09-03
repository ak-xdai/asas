"""DR 0003: `action` replaces `kind`, the four axes, deviation-only config.

- ``notification.kind`` → ``action`` (nullable — ad hoc emits carry no action)
  and ``category`` → ``nature``; new ``topic`` (nullable on historical rows —
  no backfill: topic governs *future* routing and feed filtering, delivered
  rows do not re-route), ``data`` (JSON presentation payload), ``template``.
- New config tables ``notification_topic`` and ``notification_channel_policy``
  (deviation-only; platform rows have ``org_id NULL``, org override rows beat
  them — DR 0001's shared-with-overrides pattern), with one seeded platform
  topic ``general`` — the designated home for ad hoc emits and the legacy
  ``register_kind`` shim.

Index create/drop on ``notification`` follows the 0003 house pattern in full:
existence-guarded, built CONCURRENTLY on Postgres (the table can be large and
hot; a plain build blocks every write), with interrupted-build INVALID leftovers
rebuilt. Column renames and the new empty config tables are plain DDL — cheap
metadata operations on both engines. Downgrade backfills NULL ``action`` rows
(ad hoc emits) before restoring the NOT NULL ``kind`` column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-03

"""
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names(table: str) -> set:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _pg_index_valid(name: str) -> bool:
    # Schema-qualified exactly like 0003's helper: a bare ``::regclass`` cast
    # resolves through search_path and can read a same-named index from another
    # schema on multi-schema hosts.
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


def _create_live(table: str, name: str, columns: list) -> None:
    """CREATE INDEX on a live table — CONCURRENTLY on Postgres (the 0003 house
    pattern: notification can be large and hot; a plain build blocks every
    write for its duration), with a name-matching INVALID leftover from an
    interrupted concurrent build dropped and rebuilt rather than skipped."""
    if op.get_bind().dialect.name == "postgresql":
        if name in _index_names(table):
            if _pg_index_valid(name):
                return
            with op.get_context().autocommit_block():
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        with op.get_context().autocommit_block():
            op.create_index(name, table, columns, unique=False, postgresql_concurrently=True)
    else:
        if name in _index_names(table):
            return
        op.create_index(name, table, columns, unique=False)


def _drop_live(table: str, name: str) -> None:
    if name not in _index_names(table):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.alter_column(
            "kind", new_column_name="action", existing_type=sa.String(), nullable=True
        )
        batch_op.alter_column(
            "category",
            new_column_name="nature",
            existing_type=sa.Enum(
                "action", "info", "warning", name="category", native_enum=False
            ),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("topic", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("data", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("template", sa.String(), nullable=True))

    _drop_live("notification", "ix_notification_kind")
    _create_live("notification", "ix_notification_action", ["action"])
    _create_live("notification", "ix_notification_topic", ["topic"])

    op.create_table(
        "notification_topic",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("user_configurable", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "key", name="uq_notification_topic_org_key"),
    )
    with op.batch_alter_table("notification_topic", schema=None) as batch_op:
        batch_op.create_index("ix_notification_topic_org_id", ["org_id"], unique=False)
        batch_op.create_index("ix_notification_topic_key", ["key"], unique=False)

    op.create_table(
        "notification_channel_policy",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column(
            "urgency",
            sa.Enum("low", "normal", "high", name="urgency", native_enum=False),
            nullable=True,
        ),
        sa.Column(
            "nature",
            sa.Enum("action", "info", "warning", name="nature", native_enum=False),
            nullable=True,
        ),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("mandatory", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(topic IS NOT NULL AND urgency IS NULL AND nature IS NULL) OR "
            "(topic IS NULL AND (urgency IS NOT NULL OR nature IS NOT NULL))",
            name="ck_notification_channel_policy_one_condition",
        ),
    )
    with op.batch_alter_table("notification_channel_policy", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_channel_policy_org_id", ["org_id"], unique=False
        )
        batch_op.create_index(
            "ix_notification_channel_policy_topic", ["topic"], unique=False
        )

    # Seed the designated default topic (platform row). bulk_insert renders
    # engine-correct booleans on both SQLite and Postgres.
    topic_table = sa.table(
        "notification_topic",
        sa.column("org_id", sa.Integer()),
        sa.column("key", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("user_configurable", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        topic_table,
        [
            dict(
                org_id=None,
                key="general",
                name="General",
                description="Ad hoc and uncategorized notifications",
                user_configurable=True,
                sort_order=0,
                created_at=now,
                updated_at=now,
            )
        ],
    )


def downgrade() -> None:
    op.drop_table("notification_channel_policy")
    op.drop_table("notification_topic")
    _drop_live("notification", "ix_notification_topic")
    _drop_live("notification", "ix_notification_action")
    # 0.16 ad hoc emits legitimately write action = NULL; the 0.15 column is
    # NOT NULL, so they must be backfilled before the rename or the downgrade
    # dies mid-flight (half-reverted on engines without transactional DDL).
    op.execute(sa.text("UPDATE notification SET action = 'ad_hoc' WHERE action IS NULL"))
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_column("template")
        batch_op.drop_column("data")
        batch_op.drop_column("topic")
        batch_op.alter_column(
            "nature",
            new_column_name="category",
            existing_type=sa.Enum(
                "action", "info", "warning", name="nature", native_enum=False
            ),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "action", new_column_name="kind", existing_type=sa.String(), nullable=False
        )
    _create_live("notification", "ix_notification_kind", ["kind"])
