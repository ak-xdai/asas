"""Asas Microsoft Graph — sign in as the application, talk to Graph, book Teams meetings.

Extracted from a Microsoft 365 host's working implementation (the AI Recruiter
engine), generalised: settings are an explicit object rather than a settings
import, errors are a typed hierarchy with no HTTP-status baggage, and the
client is a plain object the host owns rather than a process singleton.

Public surface — an **AI-tier** package: it fills none of the four host-contract
slots (no routers, no schema, no seeding, no ``configure_*`` globals). Everything
is an object the host constructs and passes around:

- :class:`GraphSettings` — tenant/client/secret plus authority and base URL
  (national clouds), ``from_env()`` for env-var hosts. Fails at construction.
- :class:`GraphClient` — ``get``/``post``/``patch``/``delete`` with a bearer
  token from a :class:`TokenProvider`; TLS pinned to certifi's trust store;
  ``None`` for empty successes; :class:`GraphRequestError` on 4xx/5xx.
- :class:`ClientCredentialTokenProvider` (MSAL app-only, cached) and
  :class:`StaticTokenProvider`; any object with ``async access_token()`` fits.
- :class:`TeamsMeetings` — ``create``/``reschedule``/``cancel`` on an
  organiser's calendar; :class:`Attendee`, :class:`Meeting`.
- Errors: :class:`GraphError` › :class:`GraphConfigError`,
  :class:`GraphAuthError`, :class:`GraphRequestError`, :class:`MeetingError` ›
  :class:`MeetingCreationError`, :class:`MeetingUpdateError`,
  :class:`MeetingCancelError`.
"""

from __future__ import annotations

from .auth import ClientCredentialTokenProvider, StaticTokenProvider, TokenProvider
from .client import GraphClient, default_ssl_context
from .errors import (
    GraphAuthError,
    GraphConfigError,
    GraphError,
    GraphRequestError,
    MeetingCancelError,
    MeetingCreationError,
    MeetingError,
    MeetingUpdateError,
)
from .meetings import Attendee, Meeting, TeamsMeetings
from .settings import GraphSettings

__version__ = "0.1.0"

__all__ = [
    "Attendee",
    "ClientCredentialTokenProvider",
    "GraphAuthError",
    "GraphClient",
    "GraphConfigError",
    "GraphError",
    "GraphRequestError",
    "GraphSettings",
    "Meeting",
    "MeetingCancelError",
    "MeetingCreationError",
    "MeetingError",
    "MeetingUpdateError",
    "StaticTokenProvider",
    "TeamsMeetings",
    "TokenProvider",
    "default_ssl_context",
    "__version__",
]
