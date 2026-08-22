"""HTTP API for the lookup module — read endpoints (for any consuming app) plus admin
CRUD, built by a factory so the host injects its own DB session dependency.

Auth is composition-time: the host applies its guards when including the routers
(e.g. ``app.include_router(routers.read, dependencies=[Depends(require_user)])``);
this package never learns the host's auth model.
"""

import hashlib
from typing import Callable, NamedTuple, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlmodel import Session, select

from . import service
from .models import LookupType, SortMode
from .schemas import (
    AliasIn,
    DeprecateRequest,
    LookupTypeCreate,
    LookupTypeRead,
    LookupValueCreate,
    LookupValueList,
    LookupValueRead,
    LookupValueUpdate,
    MergeRequest,
    ResolveRequest,
    ResolveResponse,
)


class Routers(NamedTuple):
    read: APIRouter
    admin: APIRouter


def _type_read(t: LookupType) -> LookupTypeRead:
    return LookupTypeRead(
        key=t.key,
        name=t.name,
        description=t.description,
        is_open=t.is_open,
        is_hierarchical=t.is_hierarchical,
        code_system=t.code_system,
        default_sort=t.default_sort.value,
        version=t.version,
    )


def build_routers(get_session: Callable) -> Routers:
    """Build the read + admin routers against the host's FastAPI session dependency
    (a callable yielding a ``sqlmodel.Session``, e.g. Teamy's ``db.get_session``)."""

    # Read API — explicit paths (no prefix) so /lookup-types doesn't collide with
    # /lookups/{type_key}.
    router = APIRouter(tags=["lookups"])
    # Admin API — value/type management.
    admin = APIRouter(prefix="/admin", tags=["lookups-admin"])

    # ---------- Read ----------

    @router.get("/lookup-types", response_model=list[LookupTypeRead])
    def list_types(session: Session = Depends(get_session)):
        types = session.exec(select(LookupType).order_by(LookupType.key)).all()
        return [_type_read(t) for t in types]

    @router.get("/lookups/{type_key}", response_model=LookupValueList)
    def list_values(
        type_key: str,
        response: Response,
        lang: str = "en",
        active: bool = True,
        q: Optional[str] = None,
        parent: Optional[str] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=1000),
        if_none_match: Optional[str] = Header(default=None),
        session: Session = Depends(get_session),
    ):
        type_ = service.get_type(session, type_key)
        # Cheap revalidation: ETag keyed on the type version (bumped on any change)
        # and the caller's org (WXL-241 — two orgs see different value sets of one
        # type). The query shape must be in the tag too — an ETag is per
        # *representation*, and page 2 or a filtered list is a different body
        # than page 1: without this a conforming client 304-reuses page 1 for
        # every other page/filter of the same type version.
        org = service._current_org(session)
        shape = hashlib.sha1(
            f"{active}.{q or ''}.{parent or ''}.{page}.{page_size}".encode()
        ).hexdigest()[:8]
        etag = f'W/"{type_.key}.v{type_.version}.{lang}.o{org or 0}.{shape}"'
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        total, items = service.list_values(
            session, type_, lang, active, q, parent, page, page_size
        )
        response.headers["ETag"] = etag
        return LookupValueList(
            type=type_.key,
            lang=lang,
            version=type_.version,
            total=total,
            page=page,
            page_size=page_size,
            items=items,
        )

    @router.get("/lookups/{type_key}/{code}", response_model=LookupValueRead)
    def get_value(
        type_key: str,
        code: str,
        lang: str = "en",
        follow_supersede: bool = True,
        session: Session = Depends(get_session),
    ):
        type_ = service.get_type(session, type_key)
        return service.get_value_read(session, type_, code, lang, follow_supersede)

    @router.post("/lookups/{type_key}/resolve", response_model=ResolveResponse)
    def resolve(
        type_key: str, payload: ResolveRequest, session: Session = Depends(get_session)
    ):
        type_ = service.get_type(session, type_key)
        labels = service.resolve_codes(session, type_, payload.codes, payload.lang)
        return ResolveResponse(type=type_.key, lang=payload.lang, labels=labels)

    # ---------- Admin ----------

    @admin.post("/lookup-types", response_model=LookupTypeRead, status_code=201)
    def create_type(payload: LookupTypeCreate, session: Session = Depends(get_session)):
        if session.exec(select(LookupType).where(LookupType.key == payload.key)).first():
            raise HTTPException(status_code=409, detail=f"type '{payload.key}' exists")
        t = LookupType(
            key=payload.key,
            name=payload.name,
            description=payload.description,
            is_open=payload.is_open,
            is_hierarchical=payload.is_hierarchical,
            code_system=payload.code_system,
            default_sort=SortMode(payload.default_sort),
        )
        session.add(t)
        session.commit()
        session.refresh(t)
        return _type_read(t)

    @admin.post("/lookups/{type_key}", response_model=LookupValueRead, status_code=201)
    def create_value(
        type_key: str, payload: LookupValueCreate, session: Session = Depends(get_session)
    ):
        type_ = service.get_type(session, type_key)
        value = service.create_value(
            session,
            type_,
            code=payload.code,
            translations=payload.translations,
            is_default=payload.is_default,
            sort_order=payload.sort_order,
            parent_code=payload.parent_code,
            meta=payload.meta,
            aliases=payload.aliases,
        )
        return service.get_value_read(session, type_, value.code, "en", False)

    @admin.patch("/lookups/{type_key}/{code}", response_model=LookupValueRead)
    def update_value(
        type_key: str,
        code: str,
        payload: LookupValueUpdate,
        session: Session = Depends(get_session),
    ):
        type_ = service.get_type(session, type_key)
        value = service.update_value(
            session,
            type_,
            code,
            translations=payload.translations,
            is_default=payload.is_default,
            sort_order=payload.sort_order,
            meta=payload.meta,
        )
        return service.get_value_read(session, type_, value.code, "en", False)

    @admin.post("/lookups/{type_key}/{code}/deprecate", response_model=LookupValueRead)
    def deprecate_value(
        type_key: str,
        code: str,
        payload: DeprecateRequest,
        session: Session = Depends(get_session),
    ):
        type_ = service.get_type(session, type_key)
        service.deprecate_value(
            session, type_, code, valid_to=payload.valid_to, superseded_by=payload.superseded_by
        )
        return service.get_value_read(session, type_, code, "en", False)

    @admin.post("/lookups/{type_key}/{code}/aliases", response_model=LookupValueRead)
    def add_alias(
        type_key: str, code: str, payload: AliasIn, session: Session = Depends(get_session)
    ):
        type_ = service.get_type(session, type_key)
        service.add_alias(session, type_, code, payload.alias, payload.lang)
        return service.get_value_read(session, type_, code, "en", False)

    @admin.delete("/lookups/{type_key}/{code}/aliases/{alias}", response_model=LookupValueRead)
    def remove_alias(
        type_key: str, code: str, alias: str, session: Session = Depends(get_session)
    ):
        type_ = service.get_type(session, type_key)
        service.remove_alias(session, type_, code, alias)
        return service.get_value_read(session, type_, code, "en", False)

    @admin.post("/lookups/{type_key}/{code}/merge", response_model=LookupValueRead)
    def merge_value(
        type_key: str,
        code: str,
        payload: MergeRequest,
        session: Session = Depends(get_session),
    ):
        type_ = service.get_type(session, type_key)
        target = service.merge_values(session, type_, code, payload.into)
        return service.get_value_read(session, type_, target.code, "en", False)

    return Routers(read=router, admin=admin)
