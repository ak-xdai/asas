"""DO NOT COPY THIS FILE. It is not authentication.

=============================================================================
This is a static token→row lookup with no secret, no expiry, no revocation,
no rotation, no hashing, and no rate limiting. Tokens are hardcoded in the
source. Anyone holding this file holds every account.

It exists so the reference host can demonstrate the *composition seam*, and
for no other reason. Authentication is deliberately NOT an Asas package
(design record 0030 §4): it is the one concern where every host differs and
where a shared implementation would be actively harmful.

If you are adopting Asas: delete this file and use your own
``get_current_user``. The only thing worth copying is the shape — a callable
that returns your user object, wired in the three places marked below.
=============================================================================

Demonstrates: the **auth composition seam**, which has no worked example
anywhere else. Three things a host must supply, and where they land:

1. ``get_current_user`` — your dependency. Asas packages never learn how it
   works; they receive the resolved object and ask the access package about it.
2. **Guards at include time** — routers come back from ``build_routers`` /
   ``build_router`` unguarded, and the host applies its own dependencies when
   including them. See ``main.py``.
3. ``configure_org_resolver`` — tenancy is a host concept. See
   ``wiring/lookups.py``.

The module refuses to arm without ``ENABLE_FAKE_AUTH=1`` so that a host which
copied it by accident fails closed on its first request rather than shipping
with a public back door.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from .config import settings
from .db import get_session
from .models import Agent

# Token → agent email. A real host would not have this table, in either sense.
FAKE_TOKENS: dict[str, str] = {
    "token-admin": "admin@example.invalid",
    "token-agent": "agent@example.invalid",
    "token-viewer": "viewer@example.invalid",
}

# The agents those tokens name, so the app is explorable by hand. Roles are
# chosen to span what the policy actually distinguishes: `admin` clears the
# implicit floor and holds unconfigured verbs, `member` holds the seeded grants,
# `viewer` holds neither and is the one that gets `internal_note` redacted.
DEMO_AGENTS: tuple[tuple[str, str, str], ...] = (
    ("Ada Admin", "admin@example.invalid", "admin"),
    ("Sam Agent", "agent@example.invalid", "member"),
    ("Vic Viewer", "viewer@example.invalid", "viewer"),
)


def seed_demo_agents(session: Session) -> None:
    """Create the agents ``FAKE_TOKENS`` names — only when fake auth is armed.

    Gated rather than unconditional: these rows exist so a human can exercise
    the permission seams from a terminal, and a host running without fake auth
    has no use for three accounts nobody can sign in as.

    Idempotent, like every other seed in the boot sequence.
    """
    if not settings.enable_fake_auth:
        return
    for name, email, role in DEMO_AGENTS:
        if session.exec(select(Agent).where(Agent.email == email)).first():
            continue
        session.add(Agent(name=name, email=email, role=role))
    session.commit()


def _token_from(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> Optional[Agent]:
    """Resolve the caller, or ``None`` when nobody is signed in.

    Returning ``None`` rather than raising is deliberate: this host runs open by
    default so that ``uvicorn app.main:app`` against an empty environment is
    actually usable. The access package treats an anonymous caller as holding no
    principals, so "open" still means "no elevated rights", not "no rules".
    """
    if not settings.enable_fake_auth:
        return None
    token = _token_from(request)
    if token is None:
        return None
    email = FAKE_TOKENS.get(token)
    if email is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown token"
        )
    return session.exec(select(Agent).where(Agent.email == email)).first()


def require_user(user: Optional[Agent] = Depends(get_current_user)) -> Agent:
    """The guard applied at ``include_router`` time for admin surfaces.

    This is the seam's second half: the *package* ships an unguarded router, the
    *host* decides who may reach it. A package that shipped its own guard would
    be making an authentication decision on behalf of every future host.
    """
    if not settings.enable_fake_auth:
        # Open mode: the whole app is anonymous, so an admin router guard has
        # nothing to check. Stated explicitly rather than left to fall through.
        return None  # type: ignore[return-value]
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    return user
