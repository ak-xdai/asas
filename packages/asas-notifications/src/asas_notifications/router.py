"""HTTP API for the recipient's own feed.

``build_router(get_session)`` is the Asas host-contract factory: the host passes
its FastAPI session dependency and applies its own auth guards at include time
(the package stays auth-free). The current user comes from the context resolver
the host configures (``configure_context_resolver``). When that resolver also
supplies an org, the package scopes every feed/read/archive query to it —
defense in depth on top of (never instead of) the host's own tenancy layer; a
cross-org id probe 404s exactly like a missing row. Without a resolver (or
outside a request) queries scope on ``user_id`` alone, as before 0.13.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from . import service
from .models import Category
from .schemas import (
    ArchiveResult,
    NotificationList,
    NotificationRead,
    ReadAllResult,
)


def _require_recipient(session: Session) -> int:
    user_id = service.current_user_id(session)
    if user_id is None:
        # An anonymous run (host enforcement off) has no "me" to have a feed.
        raise HTTPException(status_code=401, detail="No authenticated user")
    return user_id


def build_router(get_session) -> APIRouter:
    router = APIRouter(prefix="/me/notifications", tags=["notifications"])

    @router.get("", response_model=NotificationList)
    def list_notifications(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        state: Literal["open", "archived", "all"] = Query(
            "open", description="open = still in the inbox; archived = filed away"
        ),
        unread_only: bool = False,
        category: Optional[Category] = Query(
            None, description="action | info | warning — composes with state"
        ),
        session: Session = Depends(get_session),
    ):
        """The filters compose, and each one is independent: a host can ask for
        open + action (Teamy's "needs action"), unread + action, archived + action,
        and so on. `total` follows the filters; `unread_count` never does.

        Pagination happens in SQL via :func:`service.list_feed` — the feed used
        to fetch every matching row and slice in Python — and queries are
        org-scoped when the context resolver supplies an org (see the module
        docstring)."""
        user_id = _require_recipient(session)
        rows, total = service.list_feed(
            session,
            user_id,
            state=state,
            unread_only=unread_only,
            category=category,
            page=page,
            page_size=page_size,
        )
        return NotificationList(
            items=[NotificationRead.model_validate(n) for n in rows],
            total=total,
            unread_count=service.unread_count(session, user_id),
        )

    @router.post("/{notification_id}/read", response_model=NotificationRead)
    def mark_read(notification_id: int, session: Session = Depends(get_session)):
        user_id = _require_recipient(session)
        n = service.mark_read(session, user_id, notification_id)
        if n is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        return NotificationRead.model_validate(n)

    @router.post("/read-all", response_model=ReadAllResult)
    def mark_all_read(session: Session = Depends(get_session)):
        user_id = _require_recipient(session)
        return ReadAllResult(updated=service.mark_all_read(session, user_id))

    @router.post("/{notification_id}/archive", response_model=NotificationRead)
    def archive(notification_id: int, session: Session = Depends(get_session)):
        user_id = _require_recipient(session)
        n = service.archive(session, user_id, notification_id)
        if n is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        return NotificationRead.model_validate(n)

    @router.post("/{notification_id}/unarchive", response_model=NotificationRead)
    def unarchive(notification_id: int, session: Session = Depends(get_session)):
        user_id = _require_recipient(session)
        n = service.unarchive(session, user_id, notification_id)
        if n is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        return NotificationRead.model_validate(n)

    @router.post("/archive-read", response_model=ArchiveResult)
    def archive_read(session: Session = Depends(get_session)):
        user_id = _require_recipient(session)
        return ArchiveResult(updated=service.archive_read(session, user_id))

    return router
