"""HTTP API for the recipient's own feed.

``build_router(get_session)`` is the Asas host-contract factory: the host passes
its FastAPI session dependency and applies its own auth guards at include time
(the package stays auth-free). The current user comes from the context resolver
the host configures (``configure_context_resolver``); org scoping is the host's
concern (e.g. a tenancy listener filtering the feed to the token's org).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlmodel import Session, select

from . import service
from .models import Category, Notification
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

        Pagination happens in SQL (COUNT + LIMIT/OFFSET) — the feed used to fetch
        every matching row and slice in Python. Queries are additionally
        org-scoped when the context resolver supplies an org (defense in depth;
        the host's tenancy layer remains the first line)."""
        user_id = _require_recipient(session)
        conditions = [Notification.user_id == user_id]
        org_id = service.current_org_id(session)
        if org_id is not None:
            conditions.append(Notification.org_id == org_id)
        if state == "open":
            conditions.append(Notification.archived_at.is_(None))
        elif state == "archived":
            conditions.append(Notification.archived_at.is_not(None))
        if unread_only:
            conditions.append(Notification.read_at.is_(None))
        if category is not None:
            conditions.append(Notification.category == category)
        total = session.exec(
            select(sa_func.count()).select_from(Notification).where(*conditions)
        ).one()
        rows = session.exec(
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
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
