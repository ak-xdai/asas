"""Composite indexes for the feed, badge, and dispatcher scans.

The feed reads ``WHERE user_id = ? AND archived_at IS (NOT) NULL ORDER BY
created_at DESC`` and the badge counts ``user_id + read_at IS NULL + archived_at
IS NULL`` — both previously leaned on the single-column ``user_id`` index and
sorted/filtered the rest per row. The dispatcher scans ``status`` plus the
stale-claim cutoff on ``claimed_at``. Each composite's leading column covers the
single-column index it replaces, so those are dropped rather than kept as dead
weight.

Pure index DDL — no table or data changes, safe on live rows.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_user_archived_created",
            ["user_id", "archived_at", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_notification_user_read_archived",
            ["user_id", "read_at", "archived_at"],
            unique=False,
        )
        batch_op.drop_index("ix_notification_user_id")
    with op.batch_alter_table("notification_delivery", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_delivery_status_claimed",
            ["status", "claimed_at"],
            unique=False,
        )
        batch_op.drop_index("ix_notification_delivery_status")


def downgrade() -> None:
    with op.batch_alter_table("notification_delivery", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_delivery_status", ["status"], unique=False
        )
        batch_op.drop_index("ix_notification_delivery_status_claimed")
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.create_index("ix_notification_user_id", ["user_id"], unique=False)
        batch_op.drop_index("ix_notification_user_read_archived")
        batch_op.drop_index("ix_notification_user_archived_created")
