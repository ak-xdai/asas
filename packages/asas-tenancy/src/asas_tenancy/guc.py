"""Handing the tenant to Postgres: the session variable RLS policies read.

The policies written by :mod:`asas_tenancy.policy` compare each row's tenant
column against ``current_setting('app.tenant_id', true)``. This module is what
puts a value there. One call at the start of every unit of work, and every query
in that transaction is scoped by the database itself, which is why repositories
never re-filter by tenant.

Three details that are easy to get wrong and are fixed here:

* **It is a bound parameter, via ``set_config``, never string interpolation.**
  ``SET LOCAL app.tenant_id = '...'`` cannot take a bind, so a host writing the
  obvious thing ends up formatting a value into SQL. The value arrives from a
  verified credential rather than from a caller, so this is defence in depth
  rather than the front line, but the front line has been wrong before.

* **``local=True`` means "for this transaction", and outside a transaction it
  does nothing.** That is the right default (a pooled connection must not carry
  one request's tenant into the next), but it makes the pin a silent no-op if
  you call it on a connection in autocommit. Where a whole connection really is
  dedicated to one tenant, pass ``local=False`` and see
  :func:`asas_tenancy.engine.tenant_engine`, which does exactly that and then
  disposes the pool.

* **The statement is built once per GUC name.** Only the bind varies, and this
  runs on every tenant-scoped request.

**Off Postgres the pin is a no-op**, the same rule the policy helpers follow:
``set_config`` is a Postgres function, so a host running its suite on SQLite
would otherwise fail on the pin rather than on the absent isolation. The pin
going quiet there is honest, because there is no policy to feed either; what
must not go quiet is the claim that isolation exists, which is
:mod:`asas_tenancy.conformance`'s job.

The async variant exists because the sync one cannot serve an async host at all,
and it deliberately does not import ``AsyncSession``: it just awaits ``execute``,
so this module adds no dependency on greenlet or on an async driver.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

#: The session variable RLS policies read. Overridable per call, because a host
#: adopting this into an existing schema has whatever name its policies already
#: use; the default is the one this package's own helpers write.
DEFAULT_GUC = "app.tenant_id"

_STATEMENTS: dict[tuple[str, bool], TextClause] = {}

#: Dialects with settable session variables. Mirrors
#: :data:`asas_tenancy.policy._RLS_DIALECTS`, and deliberately a separate
#: constant: these are two different database features and a dialect could one
#: day have one without the other.
_GUC_DIALECTS = frozenset({"postgresql"})


def supports_guc(connectable: Any) -> bool:
    """Whether ``connectable``'s database has settable session variables.

    Walks the several shapes a caller may pass (a Connection has ``dialect``, a
    Session answers ``get_bind()``, an async session answers either) rather than
    demanding one type, because this module's whole point is not to care which.
    An object that answers none of them is treated as capable, so a shape nobody
    anticipated fails loudly on the statement rather than silently skipping the
    pin, which would look exactly like working isolation and be the opposite.
    """
    dialect = getattr(connectable, "dialect", None)
    if dialect is None:
        bind = getattr(connectable, "bind", None)
        if bind is None:
            getter = getattr(connectable, "get_bind", None)
            if callable(getter):
                try:
                    bind = getter()
                except Exception:  # noqa: BLE001 - fall through to "capable"
                    bind = None
        dialect = getattr(bind, "dialect", None)
    if dialect is None:
        return True
    return dialect.name in _GUC_DIALECTS


def _statement(guc: str, local: bool) -> TextClause:
    key = (guc, local)
    stmt = _STATEMENTS.get(key)
    if stmt is None:
        # `set_config`'s first argument cannot itself be a bind, so the GUC name
        # is interpolated. It is a package constant or a host's own boot-time
        # setting, never request input, and `validate_guc_name` refuses anything
        # that is not a bare dotted identifier.
        validate_guc_name(guc)
        stmt = text(f"SELECT set_config('{guc}', :tenant_id, {str(local).lower()})")
        _STATEMENTS[key] = stmt
    return stmt


def validate_guc_name(guc: str) -> None:
    """Refuse a GUC name that is not a bare dotted identifier.

    The name is interpolated into SQL (``set_config`` takes no bind for it), so
    this is the check that keeps that safe even if a host ever wires the name to
    something less trustworthy than a constant.
    """
    parts = guc.split(".")
    if len(parts) != 2 or not all(
        part and part.replace("_", "").isalnum() and not part[0].isdigit()
        for part in parts
    ):
        raise ValueError(
            f"Not a usable GUC name: {guc!r}. Postgres requires a custom setting "
            f"to be 'prefix.name', and this package requires both halves to be "
            f"plain identifiers."
        )


def set_tenant_guc(connectable: Any, tenant_id: Any, *, guc: str = DEFAULT_GUC,
                   local: bool = True) -> None:
    """Pin ``tenant_id`` for the current transaction on a sync session or connection.

    Call it first in the unit of work, before any query. ``tenant_id`` is
    stringified here: at the database boundary a GUC is text whatever the host's
    tenant column is, and the policy casts it back.

    A no-op off Postgres, so a dual-engine host's code path is the same on both.
    """
    if not supports_guc(connectable):
        return
    connectable.execute(_statement(guc, local), {"tenant_id": str(tenant_id)})


async def async_set_tenant_guc(connectable: Any, tenant_id: Any, *,
                               guc: str = DEFAULT_GUC, local: bool = True) -> None:
    """The same pin for an async session or connection.

    Typed loosely on purpose: awaiting ``execute`` is the whole contract, so this
    module never imports the async session type and the package stays installable
    without greenlet or an async driver.
    """
    if not supports_guc(connectable):
        return
    await connectable.execute(_statement(guc, local), {"tenant_id": str(tenant_id)})


def read_tenant_guc(connectable: Any, *, guc: str = DEFAULT_GUC) -> str | None:
    """What the database currently thinks the tenant is, or ``None``.

    For diagnostics and for the conformance kit. An unset custom GUC read with
    ``missing_ok`` comes back as ``NULL``; an empty string is normalised to
    ``None`` too, because that is what a cleared pin looks like and the policies
    treat the two the same (see :mod:`asas_tenancy.policy`).

    ``None`` off Postgres, where there is nothing to read.
    """
    if not supports_guc(connectable):
        return None
    validate_guc_name(guc)
    value = connectable.execute(
        text(f"SELECT current_setting('{guc}', true)")
    ).scalar()
    return value or None
