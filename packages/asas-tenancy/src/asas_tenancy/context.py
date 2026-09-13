"""The request-scoped tenant context: which tenant the current work belongs to.

**The tenant comes from a verified credential, never from the request.** Not the
body, not the query string, not a path segment, not a header a caller controls.
This module holds the answer for the duration of one request or one task, and
:mod:`asas_tenancy.guc` is what hands it to Postgres.

The one design decision worth stating: :func:`current_tenant_id` **raises** when
nothing is bound. The alternative, returning ``None``, reads the same at every
call site and behaves catastrophically differently, because a query built with
no tenant does not fail, it returns everybody's rows. A host that genuinely has
tenant-free work says so explicitly with :func:`maybe_tenant_id`.

Tenant ids are deliberately untyped here (``int``, ``UUID``, ``str``, whatever
the host's own tenant table uses). The context stores the value the host bound
and gives it back unchanged; only :mod:`asas_tenancy.guc` stringifies it, at the
database boundary where everything is text anyway.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Optional

_current_tenant: ContextVar[Optional[Any]] = ContextVar(
    "asas_tenancy_current", default=None
)


class TenantContextError(RuntimeError):
    """Tenant-scoped work was attempted with no tenant bound.

    Deliberately a loud failure rather than a falsy return: see the module
    docstring. If you see this from a background task, the task forgot to bind
    the tenant it is working for (:func:`tenant_scope`).
    """


def bind_tenant(tenant_id: Any) -> Token:
    """Bind the tenant for this context. Returns the token :func:`reset` takes.

    Called once per request by the host's middleware, from the verified
    credential. Callers that need to restore the previous value (a nested task,
    a test fixture) should prefer :func:`tenant_scope`.
    """
    if tenant_id is None:
        raise TenantContextError(
            "bind_tenant(None) is not a way to clear the tenant: it would make "
            "every later current_tenant_id() raise as though nothing had been "
            "bound at all. Use clear_tenant()."
        )
    return _current_tenant.set(tenant_id)


def clear_tenant() -> None:
    """Unbind the tenant. After this, :func:`current_tenant_id` raises again."""
    _current_tenant.set(None)


def reset(token: Token) -> None:
    """Restore whatever was bound before the :func:`bind_tenant` that made
    ``token``. Use this rather than :func:`clear_tenant` when unwinding, or a
    nested scope wipes its parent's tenant on the way out."""
    _current_tenant.reset(token)


def current_tenant_id() -> Any:
    """The bound tenant, or raise.

    Every read and write that is supposed to be tenant-scoped should reach the
    tenant through here, so that "no tenant" can never be silently read as "all
    tenants".
    """
    tenant_id = _current_tenant.get()
    if tenant_id is None:
        raise TenantContextError(
            "No tenant bound to this context. The tenant comes from the verified "
            "credential (middleware for a request, an explicit tenant_scope for "
            "a background task), never from the request body, query or path."
        )
    return tenant_id


def maybe_tenant_id() -> Optional[Any]:
    """The bound tenant or ``None``, for the few callers that legitimately run
    without one (a login route, a platform-wide registry read, a boot sweep).

    Using this is a statement that the absence is expected. Reach for
    :func:`current_tenant_id` everywhere else.
    """
    return _current_tenant.get()


@contextmanager
def tenant_scope(tenant_id: Any) -> Iterator[Any]:
    """Bind ``tenant_id`` for the duration of the block, then restore.

    The shape a background task wants: a job row carries the tenant it belongs
    to, the worker binds it around the handler, and the previous value (usually
    nothing) comes back afterwards even if the handler raises. Restoring rather
    than clearing is what makes it safe to nest.
    """
    token = bind_tenant(tenant_id)
    try:
        yield tenant_id
    finally:
        reset(token)
