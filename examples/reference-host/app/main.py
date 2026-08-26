"""The reference host: a helpdesk wired to the Asas package family.

Read this file first. It is the only place the boot *sequence* exists, and the
sequence is the part that is genuinely hard to reconstruct from ten package
READMEs.

    uvicorn app.main:app --reload

That works against a completely empty environment — no database, no keys, no
configuration. See ``config.py`` for which packages are in the core tier and
which are env-gated.

Every ``wiring/<package>.py`` module names the contract row it demonstrates.
This module names the order they run in, and why.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from .config import settings
from .db import create_host_schema, engine, get_session
from .fake_auth import require_user
from .wiring import access as access_wiring
from .wiring import lookups as lookups_wiring
from .wiring import ratelimit as ratelimit_wiring
from .wiring import storage as storage_wiring
from .wiring import validation as validation_wiring

log = logging.getLogger("helpdesk")


# The packages that own schema, in the order their chains run. Order among
# themselves does not matter — the chains are independent, with no cross-package
# foreign keys — but running them as one visible step does.
def _migrate_packages() -> None:
    import asas_access
    import asas_jobs
    import asas_lookups
    import asas_notifications
    import asas_search
    import asas_workflow

    for package in (
        asas_lookups,
        asas_access,
        asas_workflow,
        asas_notifications,
        asas_jobs,
        asas_search,
    ):
        package.migrate(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """The boot sequence. The numbering is the contract.

    Steps 2 and 3 look interchangeable and are not. Storage is configured
    *before* anything touches the database because the first caller of
    ``storage()`` is usually a seed or a migration, not a request — and
    ``asas_storage`` raises rather than falling back to disk, so a late
    ``configure()`` turns into a boot crash that points at the wrong place.

    Steps 4 and 5 are the other ordering that matters: seeds write rows the
    ``configure_*`` hooks decide the shape of (an org resolver decides which org
    a seeded row belongs to), so hooks precede seeds.
    """
    # 1. The host's own schema, first. `migrate()` is adopt-or-create and looks
    #    for its own tables to decide whether it is adopting; on a brownfield
    #    database those tables come from the host's history.
    create_host_schema(engine)

    # 2. Storage — before anything that might store a byte. See the docstring.
    storage_wiring.configure()

    # 3. Package schemas, after the host's own.
    _migrate_packages()

    # 4. `configure_*` hooks and registrations: declare what exists. These
    #    validate, so a typo'd field or a rule naming an unknown field fails the
    #    boot here rather than misbehaving quietly later.
    lookups_wiring.configure()
    access_wiring.configure()
    validation_wiring.configure()
    ratelimit_wiring.configure()

    # 5. Seeds, last, and idempotent — safe on every boot.
    from sqlmodel import Session

    with Session(engine) as session:
        lookups_wiring.seed(session)
        access_wiring.seed(session)

    for name, state in settings.tier_report().items():
        log.info("helpdesk: %-9s %s", name, state)

    yield

    # Nothing to tear down: no package holds a resource the process does not
    # already own. Stated rather than omitted, so the absence reads as a fact.


app = FastAPI(
    title="Asas reference host",
    description=(
        "A helpdesk wired to the Asas package family. This is a conformance "
        "harness that reads as an example — see CLAUDE.md before copying "
        "anything out of it."
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Which tier each package is running in.

    A host that degrades silently is indistinguishable from one that is broken,
    so the reduced configuration announces itself.
    """
    return {"status": "ok", "tiers": settings.tier_report()}


def _include_package_routers() -> None:
    """Routers come back unguarded; the **host** applies its own auth.

    This is the composition seam in its most concrete form. ``build_routers``
    hands back plain ``APIRouter`` objects, and the decision about who may reach
    the admin surface is made here — by the host, at include time — because no
    package can know a host's auth model.
    """
    import asas_lookups
    import asas_validation

    routers = asas_lookups.build_routers(get_session)

    # Read surface: open, like the rest of this app in its default posture.
    app.include_router(routers.read)

    # Admin surface: the host's guard, applied here and nowhere else.
    app.include_router(routers.admin, dependencies=[Depends(require_user)])

    # Rules the frontend mirrors for pre-submit feedback. Read-only, no guard.
    app.include_router(asas_validation.build_router())


_include_package_routers()
