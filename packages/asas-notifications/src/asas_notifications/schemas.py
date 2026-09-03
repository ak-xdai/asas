"""Read models for the /me/notifications API. Kept in sync with the TS types in
``frontend/src/lib/api.ts`` (house rule)."""

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel

from typing import Any

from .models import Nature, Reason, Urgency


class NotificationRead(SQLModel):
    id: int
    #: The application action that caused this row (DR 0003) — None for ad hoc
    #: emits and for rows predating 0.16 that were never re-labeled.
    action: Optional[str] = None
    topic: Optional[str] = None
    nature: Nature
    urgency: Urgency
    reason: Reason
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    title: str
    body: Optional[str] = None
    link: Optional[str] = None
    template: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    read_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    created_at: datetime


class NotificationList(SQLModel):
    items: list[NotificationRead]
    #: Rows matching the request's filters — the paging total, not an inbox size.
    total: int
    #: Unread and un-archived, ignoring the request's filters: the same number on
    #: every view, so a badge fed from any list call agrees with every other.
    unread_count: int


class ReadAllResult(SQLModel):
    updated: int


class ArchiveResult(SQLModel):
    updated: int
