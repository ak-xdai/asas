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

Index create/drop is guarded by inspector existence checks (adopting hosts may
carry different historical index names; partial runs stay retryable — the 0003
house pattern). Column renames and table creates are plain DDL: cheap metadata
operations on both engines, no CONCURRENTLY concerns.

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

    names = _index_names("notification")
    if "ix_notification_kind" in names:
        op.drop_index("ix_notification_kind", table_name="notification")
    names = _index_names("notification")
    if "ix_notification_action" not in names:
        op.create_index("ix_notification_action", "notification", ["action"])
    if "ix_notification_topic" not in names:
        op.create_index("ix_notification_topic", "notification", ["topic"])

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
    names = _index_names("notification")
    if "ix_notification_topic" in names:
        op.drop_index("ix_notification_topic", table_name="notification")
    if "ix_notification_action" in names:
        op.drop_index("ix_notification_action", table_name="notification")
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
    if "ix_notification_kind" not in _index_names("notification"):
        op.create_index("ix_notification_kind", "notification", ["kind"])
