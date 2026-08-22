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
- :func:`migrate` — package-owned Alembic chain, adopt-or-create; call at boot.
- ``service`` — query/resolve/admin functions taking an explicit ``Session``.
"""

from .migrate import migrate
from .router import Routers, build_routers
from .seed import seed_lookups as seed
from .service import configure_org_resolver

__version__ = "0.10.4"

__all__ = [
    "Routers",
    "build_routers",
    "configure_org_resolver",
    "migrate",
    "seed",
    "__version__",
]
