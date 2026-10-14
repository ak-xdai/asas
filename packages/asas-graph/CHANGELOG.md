# Changelog — `asas-graph`

Versions follow semver, and the git tag matches this file: `asas-graph/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 — unreleased

First release. Extracted from the AI Recruiter engine's `base/services/graph`
(`GraphClient`, `TeamsMeetingService`) and generalised — the shapes below are
the ones that survived production, with the product-specific parts removed.

- **`GraphSettings`** replaces the engine's `config.GRAPH_*` import: an explicit
  object the host builds, validated at construction (`GraphConfigError`), with
  `authority_host`/`base_url` for national clouds and an opt-in `from_env()`.
- **`GraphClient`** is a plain object rather than a process singleton, owns (or
  borrows) one `httpx.AsyncClient`, verifies TLS against certifi's bundle, and
  returns `None` for empty successes (202/204) instead of needing a separate
  `post_no_content`. Sits on a **`TokenProvider`** seam: MSAL client-credentials
  by default (`ClientCredentialTokenProvider`), `StaticTokenProvider` for tests
  and for hosts with their own token source.
- **`GraphRequestError`** now unpacks Graph's error envelope (`code`,
  `message`, `request_id`) and the `Retry-After` header (`retry_after`,
  `is_throttled`, `is_transient`, `is_not_found`). No `status_code=502` — how a
  host reports a Graph failure to its own callers is host policy.
- **`TeamsMeetings`** (`create`/`reschedule`/`cancel`) takes the organiser
  explicitly. Datetimes must be timezone-aware and are converted to UTC — the
  engine formatted whatever it was given and labelled it UTC, which is wrong for
  any non-UTC aware datetime. `end <= start` is refused. `Attendee` gains
  `required=False` for optional attendees; `Meeting` gains `web_link`.
- Depends on `httpx`, `msal`, `certifi` only. No pydantic, no FastAPI.
