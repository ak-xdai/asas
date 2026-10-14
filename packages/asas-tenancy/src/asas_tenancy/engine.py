"""A connection pool dedicated to one tenant, for work with no request behind it.

Ordinary request traffic pins the tenant per transaction
(:func:`asas_tenancy.guc.set_tenant_guc`) and that is the right default: the
connection goes back to the pool carrying nothing.

Some work has no transaction to hang that off. A boot seeder, a nightly sweep, an
outbox dispatch that issues its own SQL on raw connections. Those need the tenant
pinned at the **connection** level, and that is where the trap is: a pooled
connection carrying one tenant's session variable must never be handed to another
tenant's work. So this hands out an engine whose every connection is pinned to
one tenant, and **disposes the pool** when the block ends.

The failure it prevents is worth naming, because it is the one that looks like
nothing is wrong. A tenant-blind pass over a protected table reads an empty set
and reports success: no error, no log line, zero rows processed, and a queue that
silently never drains. It also cannot be caught locally, because a developer's
superuser connection ignores the policy (see :mod:`asas_tenancy.conformance`).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from asas_tenancy.guc import DEFAULT_GUC, validate_guc_name
from asas_tenancy.policy import has_rls


def pin_engine(engine: Engine, tenant_id: Any, *, guc: str = DEFAULT_GUC) -> Engine:
    """Pin ``tenant_id`` on every connection this engine opens, from now on.

    Session-level, not transaction-level, so it survives across the several
    transactions a sweep runs. The listener fires on real DBAPI connect, which
    means a connection already in the pool is not retro-pinned: pass a fresh
    engine, which is what :func:`tenant_engine` does.

    Returns the engine, so it can be used inline.
    """
    if not has_rls(engine.dialect.name):
        return engine
    validate_guc_name(guc)
    value = str(tenant_id)

    @event.listens_for(engine, "connect")
    def _pin(dbapi_connection, _record):  # pragma: no cover - driver callback
        cursor = dbapi_connection.cursor()
        try:
            # A raw DBAPI cursor, so this is paramstyle-dependent; every
            # supported driver takes pyformat or format for a text query, and
            # the value is still a bound parameter rather than interpolated.
            cursor.execute(f"SELECT set_config('{guc}', %s, false)", (value,))
        finally:
            cursor.close()

    return engine


@contextmanager
def tenant_engine(url: str, tenant_id: Any, *, guc: str = DEFAULT_GUC,
                  **engine_kwargs: Any) -> Iterator[Engine]:
    """An engine whose connections all belong to ``tenant_id``, disposed on exit.

    ``engine_kwargs`` go to ``create_engine``. A small pool is usually right: this
    is background work, and the pool is thrown away at the end of the block.

        for tenant in tenants:
            with tenant_engine(dsn, tenant) as engine:
                dispatch_outbox(engine)

    Disposing is not tidiness, it is the isolation: without it a pinned
    connection outlives the block and the next tenant's pass can be handed it.
    """
    engine = create_engine(url, **engine_kwargs)
    try:
        pin_engine(engine, tenant_id, guc=guc)
        yield engine
    finally:
        engine.dispose()
