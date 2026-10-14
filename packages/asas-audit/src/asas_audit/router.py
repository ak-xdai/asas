"""The read surface: history and verification.

Two routes, and **no route that writes**. Appending is something a host's own
service does inside its own transaction (see :func:`asas_audit.append`); exposing
it over HTTP would be a way to write history that did not happen.

**Auth is composition-time**, the family's rule: the factory takes the host's
session dependency and nothing else, and the host applies its own guards when it
includes the router. In practice these are administrator surfaces, but this
package has no way to know what an administrator is in the host's model, and
guessing would be worse than leaving it to the caller.

The tenant is not a parameter. It comes from the host's own tenant context, which
is what the row-level security policy reads, so a caller cannot ask for another
tenant's history by naming it. That is the same rule as everywhere else: the
tenant comes from a verified credential, never from the request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import HTTPException, APIRouter, Depends, Query
from pydantic import BaseModel

from asas_audit import service


class AuditEventRead(BaseModel):
    """One entry as the API states it.

    The fingerprints are hex rather than raw bytes so the response is JSON, and
    they are exposed at all so a reader can check the chain themselves rather
    than trust :class:`VerifyReportRead`.
    """

    id: str
    seq: int
    actor: str
    action: str
    resource_type: str
    resource_id: str
    payload: dict[str, Any]
    occurred_at: datetime
    hash_current: str


class ChainBreakRead(BaseModel):
    event_id: str
    seq: int
    action: str
    expected_hash: str
    stored_hash: str
    detail: str


class VerifyReportRead(BaseModel):
    events_checked: int
    is_intact: bool
    breaks: list[ChainBreakRead]


def build_router(get_session: Callable, *, tenant: Optional[Callable] = None) -> APIRouter:
    """The host passes its session dependency and, optionally, its tenant
    dependency.

    ``tenant`` is a dependency returning the current tenant id — a token claim,
    a context variable, or a single-tenant constant; this package does not need
    to know which. When omitted it defaults to the family's fail-closed
    resolver, ``asas_tenancy.current_tenant_id``, which RAISES when no tenant
    is bound: a hand-rolled wrapper returning ``None`` would instead filter on
    ``org_id == 'None'`` and serve an empty history plus an "intact chain of 0
    events" — a silently wrong audit surface.
    """
    if tenant is None:
        from asas_tenancy import current_tenant_id

        def tenant() -> str:  # fail-closed default
            return str(current_tenant_id())
    router = APIRouter(prefix="/audit", tags=["audit"])

    @router.get("/events", response_model=list[AuditEventRead])
    def list_events(
        session=Depends(get_session),
        org_id=Depends(tenant),
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[AuditEventRead]:
        if resource_id is not None and resource_type is None:
            # The service refuses this pair with a ValueError; a client input
            # mistake must answer 400, never a 500 with a server traceback.
            raise HTTPException(
                status_code=400,
                detail="resource_id requires resource_type",
            )
        rows = service.history(
            session,
            org_id=org_id,
            resource_type=resource_type,
            resource_id=resource_id,
            actor=actor,
            action=action,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return [
            AuditEventRead(
                id=r.id,
                seq=r.seq,
                actor=r.actor,
                action=r.action,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                payload=r.payload,
                occurred_at=r.occurred_at,
                hash_current=bytes(r.hash_current).hex(),
            )
            for r in rows
        ]

    @router.get("/verify", response_model=VerifyReportRead)
    def verify_chain(
        session=Depends(get_session), org_id=Depends(tenant)
    ) -> VerifyReportRead:
        report = service.verify(session, org_id)
        return VerifyReportRead(
            events_checked=report.events_checked,
            is_intact=report.is_intact,
            breaks=[
                ChainBreakRead(
                    event_id=str(b.event_id),
                    seq=b.seq,
                    action=b.action,
                    expected_hash=b.expected_hash_hex,
                    stored_hash=b.stored_hash_hex,
                    detail=b.detail,
                )
                for b in report.breaks
            ],
        )

    return router
