"""Asas lookups — generic bilingual reference-data engine.

A four-table core (type registry → value → translation, plus alias) that scales to
many lookup types without a table per type: stable codes in consuming records,
labels as translations, aliases for search, per-org value overlays on top of
platform seeds. Extracted from Teamy (DR 0017 pilot, epic TEAMY-466).

Public surface — the Asas host contract:

- :func:`build_routers` — read + admin ``APIRouter``s built against the host's
  session dependency; the host applies its own auth guards when including them.
- :func:`configure_org_resolver` — optional multi-tenancy hook (how to read the
  acting org off a session). Unconfigured ⇒ single-tenant: global rows only.
- :func:`seed` — idempotent starter reference data; call at boot after ``migrate``.
- :func:`seed_file` — the same, for a HOST's own vocabulary in its own JSON file.
- :class:`OrmSeeder` — the same four tables written through the ORM's object
  graph instead, on SQLite. See ``orm_seeding`` for why both exist.
- :func:`migrate` — package-owned Alembic chain, adopt-or-create; call at boot.
- ``service`` — query/resolve/admin functions taking an explicit ``Session``.
"""

from .migrate import migrate
from .models import TypeScope  # noqa: F401
from .router import Routers, build_routers
from .orm_seeding import Inspector, OrmSeeder, SeedReport  # noqa: F401
from .seeding import (  # noqa: F401
    bump_version_if,
    ensure_type,
    ensure_value,
    seed_file,
    seed_lookups as seed,
    seed_org_lookups,
)
from .service import configure_org_resolver, find_org_shadows

__version__ = "0.13.2"

__all__ = [
    "Inspector",
    "OrmSeeder",
    "SeedReport",
    "Routers",
    "TypeScope",
    "__version__",
    "build_routers",
    "bump_version_if",
    "configure_org_resolver",
    "ensure_type",
    "ensure_value",
    "find_org_shadows",
    "migrate",
    "seed",
    "seed_file",
    "seed_org_lookups",
]
