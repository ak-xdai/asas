# asas-graph

Microsoft Graph for a Microsoft 365 tenant, app-only: sign in **as the
application** (client credentials), call Graph with typed errors, and book,
move and cancel Teams meetings in an organiser's calendar. The certificate
trust-store problem ("unable to get local issuer certificate" on machines whose
OpenSSL bundle is empty) is solved once, here.

Nothing in it is about any one product. It is an **AI-tier** Asas
package: it fills none of the four host-contract slots (no routers, no schema,
no seeding, no `configure_*` globals). Everything is an object the host
constructs and owns, so a process that talks to two tenants makes two clients,
and a test passes a fake transport instead of patching a module.

```python
import asas_graph

# boot (host wiring) — build settings from *your* configuration; the library reads none
graph = asas_graph.GraphClient(asas_graph.GraphSettings(
    tenant_id=settings.graph_tenant_id,
    client_id=settings.graph_client_id,
    client_secret=settings.graph_client_secret,
))
teams = asas_graph.TeamsMeetings(graph, organizer="interviews@example.gov")

# use
meeting = await teams.create(
    "Cloud Architect interview",
    start, end,                                   # timezone-aware datetimes
    attendees=[asas_graph.Attendee("chen.wei@example.com", "Chen Wei")],
    body_html="<p>Agenda…</p>",
)
meeting.id        # the calendar event id — keep it; reschedule/cancel need it
meeting.join_url  # the Teams join link

await teams.reschedule(meeting.id, new_start, new_end)
await teams.cancel(meeting.id)

# shutdown
await graph.aclose()          # or: async with asas_graph.GraphClient(...) as graph:
```

## What a host needs in the tenant

An app registration with a client secret and the **`Calendars.ReadWrite`
application** permission, admin-consented. That is all: the meeting is a
calendar event with `isOnlineMeeting=true`, so Exchange sends the invitations,
updates and cancellations itself off the calendar write. The library sends no
mail and needs no `Mail.Send` (that is `asas-mail`'s job, and it will sit on
this client). `OnlineMeetings.ReadWrite.All` is **not** needed.

`organizer` is the mailbox the events live in — a shared mailbox such as
`interviews@example.gov` works well. It must be a real, licensed mailbox.

## Settings

`GraphSettings(tenant_id, client_id, client_secret, authority_host=…, base_url=…,
scopes=…, timeout_seconds=30)`. It validates at construction, so a
misconfigured host fails at boot, not at its first meeting.

`authority_host` and `base_url` exist for national clouds — Azure Government
signs in at `login.microsoftonline.us` and calls `graph.microsoft.us`; the
defaults are the global cloud. `GraphSettings.from_env()` reads
`GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` (plus the optional
`GRAPH_AUTHORITY_HOST`, `GRAPH_BASE_URL`, `GRAPH_SCOPES`, `GRAPH_TIMEOUT_SECONDS`)
for hosts configured by environment; the prefix is a parameter.

## The client

`GraphClient.get / post / patch / delete` take a path relative to `base_url`
(`/users/{id}/events`) and return the decoded JSON body, or `None` when the
success carried none — `sendMail` answers 202, `PATCH` and `DELETE` answer 204,
and a first implementation usually crashes parsing the body that is not there.

Any 4xx/5xx raises `GraphRequestError` with `status`, `method`, `path`, the
decoded body as `detail`, and Graph's error envelope unpacked: `code`,
`message`, `request_id` (what Microsoft support asks for). `retry_after` is
set from the `Retry-After` header on throttling; `is_throttled`,
`is_not_found` and `is_transient` say what a caller should do. The library
does **not** retry: how many times, and whether to wait, is host policy.

TLS is verified against `certifi`'s bundle (`default_ssl_context()`), which is
the fix for the empty-trust-store failure. A host that must trust a private CA,
go through a proxy, or share a connection pool passes its own
`httpx.AsyncClient` as `http=`; the library uses it as-is and does not close it.

## Tokens

The client asks a `TokenProvider` (`async access_token() -> str`) before every
request and caches nothing itself. The default is `ClientCredentialTokenProvider`:
MSAL's confidential-client flow, one MSAL application per provider so MSAL's
in-memory cache is honoured and the token endpoint is hit only near expiry.
`StaticTokenProvider(token)` is for tests and for hosts that get tokens
elsewhere. When the shared service-token client (`asas-upstream`) exists, it
plugs in at this seam and nothing here changes.

## Meetings

`TeamsMeetings(client, organizer)` → `create`, `reschedule`, `cancel`.

- **Datetimes must be timezone-aware.** They are converted to UTC and sent with
  `timeZone: "UTC"`; Graph renders each attendee's copy in *their* zone. A
  naive datetime raises `ValueError` rather than being guessed at.
- `create(..., online=False)` books a plain calendar event with no Teams link.
- `reschedule` sends only what you pass: the window always, `subject` and
  `attendees` only when given. Passing `attendees=[]` clears them. Every PATCH
  notifies attendees and resets their responses, so compare before you call it.
- `cancel` deletes the organiser's event; Exchange sends the cancellation.
- Failures raise `MeetingCreationError` / `MeetingUpdateError` /
  `MeetingCancelError` (all `MeetingError`), with the `GraphRequestError` as
  `__cause__`.

## Errors

```
GraphError
├── GraphConfigError        the host wired it wrong (raised at construction)
├── GraphAuthError          no token could be acquired
├── GraphRequestError       Graph answered 4xx/5xx
└── MeetingError
    ├── MeetingCreationError
    ├── MeetingUpdateError
    └── MeetingCancelError
```

None of these carries an HTTP status for *your* callers; mapping a Graph
failure onto your API's responses is host policy.

## Testing a host

Pass `http=httpx.AsyncClient(transport=httpx.MockTransport(handler))` and
`token_provider=StaticTokenProvider("x")`; nothing leaves the process. The
package's own `tests/conftest.py` has a recording fake worth copying.

See the repo README for the family contract. Extracted from the AI Recruiter
engine's `base/services/graph` (roadmap row 7).
