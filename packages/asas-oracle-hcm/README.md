# asas-oracle-hcm

Oracle Fusion HCM over REST, async: a client with typed errors, the query
language's traps written down once, a read cache behind a seam, and id-to-name
lookups shaped to what a real Fusion pod will actually answer. Every product
that integrates Fusion rediscovers the same things (there is no working OR,
several ids in one query return 500, test pods scrub every work email, a
duplicate key comes back as a 400 with prose); they are solved here.

**Two layers, one package.** The FUSION core works on any Fusion REST API
(HCM, ERP, SCM alike): settings and credentials, the client and its breakers,
the query grammar, paging limits, attachments and the gateway check. The HCM
layer adds what HCM integrations share: the reference-data lookups (grades,
departments, workers, positions and the rest) and the cache default that keeps
them. An ERP integration uses the core and ignores the rest.

**It is domain-agnostic.** It knows Fusion (the query grammar, the media
types, the paging limits, attachments, and the HCM reference data every product
resolves ids against: grades, departments, business units, organizations, jobs,
positions, job families, workers) and nothing about what a product does with
it. A recruiting, core HR, absence or payroll integration builds on the same
client; which resources it reads, what a row means and which of them it caches
are the product's.

It is a **table-less, router-less**
Asas package: it fills none of the four host-contract slots (no routers, no
schema, no seeding, no `configure_*` globals). Everything is an object the host
constructs and owns, so a process that talks to two instances makes two
clients, and a test passes a fake transport instead of patching a module.

```python
import asas_oracle_hcm as oracle

# boot (host wiring): build settings from *your* configuration; the library reads none
client = oracle.OracleFusionClient(oracle.OracleSettings(
    base_url=settings.oracle_base_url,        # .../hcmRestApi/resources/11.13.18.05
    username=settings.oracle_username,
    password=settings.oracle_password,
))
lookups = oracle.OracleLookups(client)        # ONE per process: it remembers answers

# use
page = await client.get_collection(
    "/positions",
    {"q": oracle.and_(oracle.eq("BusinessUnitId", bu, quote=False),
                      oracle.like("Name", "architect")),
     "limit": 25, "totalResults": "true"},
)
page.items, page.has_more, page.total          # total is None when Oracle declines to count

names = await lookups.names("job", [row["JobId"] for row in page.items])
people = await lookups.people([row["ManagerPersonId"] for row in page.items])
where = await lookups.worker_department("jane@example.gov")   # WorkerDepartment | None

# shutdown
await client.aclose()        # or: async with oracle.OracleFusionClient(...) as client:
```

## Settings

`OracleSettings(base_url, ...)` validates at construction, so a half-wired host
fails at boot. An **empty `base_url` is allowed** and means "Oracle is off in
this deployment": `client.configured` is `False` and the first call raises
`OracleNotConfiguredError`, so a host can boot without an instance.

**Credentials, three shapes that combine** (`settings.auth()` builds them;
`settings.describe_auth()` names them without their values):

| Settings | Sends | For |
| --- | --- | --- |
| `username`, `password` | `Authorization: Basic ...` | Fusion directly, with an integration user |
| `gateway_api_key`, `gateway_api_key_header` | one header, e.g. `x-api-key` | an API gateway; alone when the gateway authenticates onward itself |
| `oauth_token_url`, `oauth_client_id`, `oauth_client_secret`, `oauth_scope` | `Authorization: Bearer <token>` | Oracle Integration Cloud directly, or a gateway that wants a token |

A `base_url` needs at least one. A gateway key combines with either of the
others; Basic and OAuth are refused together, because both own the
`Authorization` header. The OAuth token is fetched on first use, shared by
concurrent requests, reused until a minute before it expires, and renewed
after a 401, when the refused request is sent once more (it was refused before
anything ran, so that is safe even for a POST). Anything else (a managed
identity, a token the host already caches) is a credential of your own,
passed as `OracleFusionClient(settings, auth=...)`: `BearerToken(callable)`,
or any object with the two methods of the `Auth` protocol.

`extra_headers={...}` ride on every request: `REST-Framework-Version`, which
changes how Fusion shapes some answers, or a header your gateway asks for.

`max_connections` (20) bounds the pool the client owns, kept warm because the
TLS handshake to a gateway is the dear part, and `connect_retries` (2) retries
a connection that could not be opened, which sent nothing and so is safe even
for a POST. Neither applies to an `httpx.AsyncClient` you pass in.

`OracleSettings.from_env(prefix="ORACLE_HCM_")` reads `BASE_URL`, `USERNAME`,
`PASSWORD`, `GATEWAY_API_KEY`, `GATEWAY_API_KEY_HEADER`, `OAUTH_TOKEN_URL`,
`OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_SCOPE`, `TIMEOUT_SECONDS`,
`MAX_CONNECTIONS` and `CONNECT_RETRIES`.

## The client

`get / get_collection / iter_collection / get_bytes / post / patch` take a
path relative to the REST root.

- Every read sends `onlyData=true`, which strips the HATEOAS `links` (they
  triple a payload). Pass `onlyData="false"` when you need them.
- `get_collection` returns a `CollectionPage(items, has_more, total)`. `total`
  appears only with `totalResults=true`, and some resources answer `-1` even
  then: that is Oracle declining to count, read as `None`, never zero.
- `iter_collection` pages by offset until Oracle says there is no more. Give it
  a stable `orderBy` (an id) when the collection can change during the walk.
- `get_bytes` reads a binary enclosure with `Accept: */*` (an enclosure answers
  406 to a JSON Accept header).
- `post` sends plain JSON; `patch` sends
  `application/vnd.oracle.adf.resourceitem+json`. Each refuses the other's
  media type.
- A field list NARROWS a response: ask for no projection when you need the
  names Fusion denormalises beside a record's ids (a `PhaseName` beside a
  `PhaseId`), because `fields=...PhaseId` drops them and leaves ids nothing
  resolves.

**No retries.** Whether and when to retry is host policy. A PATCH by id is
idempotent and safe for a caller to retry; a transition such as
`POST .../action/move` is not, because a repeat advances the record again.

A host that needs a private CA, a proxy or a shared pool passes its own
`httpx.AsyncClient` as `http=`; the library uses it as-is and does not close it.

## Upstream health

**Circuit breakers, one per resource and one for the host**, configured by
`BreakerPolicy(failures=5, cooldown_seconds=30)` passed as `breaker=`. After
`failures` consecutive outages on a resource, READS of it raise
`OracleUnavailableError` at once (with `retry_after_seconds`) instead of each
waiting out the timeout; when the cooldown ends ONE probe goes out, and
success closes the breaker while a fault opens it again. **Writes are counted
but never refused**, because the caller's outbox owns their retry.
`BreakerPolicy(failures=0)` turns them off.

**What counts as an outage** (`is_outage`) is narrow on purpose: no response
at all, 502, 503, 504 or 429. A 500 is an ANSWER: it is what a gateway returns
for an operation registered wrong and what Fusion returns for a query it
cannot run, so counting it would let one broken endpoint switch every read
off. 401/403 are `OracleAuthError`, a configuration fault, never an outage.
Per resource for the same reason (`/positions` failing does not stop
`/grades`), while a connection failure also counts against the HOST breaker,
since when the host is down every resource is.

`client.health.snapshot()` is a JSON-ready view for a status page: the overall
state, every breaker that is not plainly closed, reads refused, and per method
and resource the calls, faults, average and worst latency.

`on_request=` is called once per request with a `RequestEvent(method,
resource, path, status, elapsed_ms, fault, refused)`, for your own metrics or
tracing (OpenTelemetry, Prometheus); an exception in it is logged and ignored.
`count_calls()` counts the Oracle requests made inside a block, which is how a
host notices a request path that has started reaching Oracle (cache hits are
not calls):

```python
with oracle.count_calls() as counted:
    response = await call_next(request)
log.info("request", path=request.url.path, oracle_calls=counted.calls)
```

`client.request(method, path, params=, json_body=, headers=,
raise_for_status=True, guard=None)` is the one building block every verb uses,
public for what they do not cover; `raise_for_status=False` returns a 4xx/5xx
response instead of raising, and `guard=False` sends even with a breaker open.

## Checking a gateway registration

A gateway registers each operation on its own, so "the gateway works" is one
fact per operation. `asas-oracle-check` (or `check(client, probes)`) calls each
read in a manifest once, with your own settings, and prints the status, a
one-line error, a row count and the time for each. It tells a missing
registration (404 from the gateway) from a refused key (401/403), a transport
the gateway was not registered for, and a fault behind it (5xx). It never
prints a body, and a write in the manifest is listed, never called.

The default manifest is the HCM reference reads the lookups use
(`REFERENCE_PROBES`). Pass your product's own with `--manifest probes.json`, or
a few reads with `--path /workers --path /absences`. A probe can take values
from an earlier answer: `provides` collects a field from the rows
(`{"PersonId": "PersonId"}`, or `"@enclosure_key"` for an attachment's key),
a later path uses them (`/workers/{PersonId}`), and `tries` lets a probe walk
several values until one answers what it provides ("the first worker with an
attachment"):

```json
{"probes": [
  {"path": "/workers", "params": {"limit": 5}, "provides": {"PersonId": "PersonId"}},
  {"path": "/workers/{PersonId}/child/attachments", "params": {"onlyData": "false"},
   "provides": {"Key": "@enclosure_key"}, "tries": 5},
  {"path": "/workers/{PersonId}/child/attachments/{Key}/enclosure/FileContents", "binary": true},
  {"label": "PATCH /workers/{PersonId}", "call": false}
]}
```

[`examples/recruiting-probes.json`](examples/recruiting-probes.json) is the
manifest for an Oracle Recruiting integration (18 reads, 2 writes listed).

```
ORACLE_HCM_BASE_URL=https://gateway.example/OIC/1.0 ORACLE_HCM_GATEWAY_API_KEY=... \
  ORACLE_HCM_GATEWAY_API_KEY_HEADER=X-Gateway-Key asas-oracle-check --manifest probes.json
```

## The query language

What a real pod does with `q`, and the helpers that keep you off the traps:

- **AND is `;`** (`and_(...)`). The word `AND` returns an empty body.
- **There is no working OR.** `OR`, `IN (...)` and a comma list all silently
  match nothing. A facet takes one value; several ids mean several requests.
- `like(field, term)` is `field LIKE '%term%'`, case-insensitive on `Title`.
- **Values are single-quoted with no escape form.** A quote would end the
  literal and a `;` would start another clause, so either changes what the
  query MEANS (an input that reads more than it should). `eq`, `like` and
  `literal` raise `OracleQueryError` for both, before anything is sent, and a
  field name must be an attribute name. `strip_quotes=True` is the explicit
  opt-in for free text, accepting that `O'Brien` is searched as `OBrien`.
- `eq(field, value)` quotes; `eq(..., quote=False)` leaves a bare value, which
  must then be a plain id token (`300000008607150`).
- **Equality is case-sensitive.**

Row readers: `text` (null reads as ""), `flag` (JSON booleans and "Y"/"N"
alike), `integer`, and `child_items` for an expanded child, which arrives as a
bare list on some resources and as `{"items": [...]}` on others.

## Lookups

`OracleLookups(client, concurrency=12, name_ttl_seconds=21600,
people_ttl_seconds=3600, store=None, kinds=None)`. Hold one per process.
`kinds` adds your own id kinds to the eight built in, as `{kind: (resource, id
field, name field)}`, for example `{"location": ("/locations", "LocationId",
"LocationName")}`; `lookups.kinds` lists them all.

- `names(kind, ids)` for `grade`, `department`, `business_unit`,
  `organization`, `job`, `position`, `job_family`, `person`
  (`NAME_LOOKUPS`), and `names_many({kind: ids})` under one concurrency budget.
  **One request per id, concurrently**, because a pod 500s on several ids in one
  query and fetching a table whole is slower still. `department` is keyed on
  `OrganizationId`, not `DepartmentId`: a department is an organization.
  `business_unit` reads `/hcmBusinessUnitsLOV` (the plain `/businessUnitsLOV`
  is a 404 on at least one pod).
- `people(person_ids)` returns `Person(person_id, display_name, address)`.
  `/publicWorkers` lists current workers only.
- `positions(position_ids)` returns `Position(position_id, name, budgeted)`:
  the name AND `BudgetedPositionFlag` in ONE request per position, instead of
  reading each position twice. `budgeted` is `None` when Oracle left the flag
  unset, which is not `False`.
- `find_worker(address, expand=None)` and `worker_department(address)`: a
  worker by email, by `WorkEmail` then `Username`, each as given and
  lowercased, and the department on their primary assignment.
- `departments_in_set(set_code, active_only=True)`: `/departments` carries no
  business unit and is partitioned by reference set (`SetCode`).

Lookups are fail-soft: a failed or unknown id is absent from the answer, and a
failure costs a name, never the request. `departments_in_set` is the exception,
because a caller syncing a catalogue must know it got the whole list.

### Keeping answers across processes

In memory, every new process and every replica asks Oracle again, one request
per id. Pass `store=` a `LookupStore` (two async methods, `read(kind, ids)` and
`write(kind, {id: (name, extra)})`, over a table keyed on `(kind, oracle_id)`)
and the lookups get three rules a store does not have to implement:

- **Stale-while-revalidate.** An answer past its freshness (the same two TTLs)
  is served at once and refreshed in the background, so after the first sight
  of an id no caller waits on Oracle for it again. `await lookups.drain()` waits
  for refreshes in flight.
- **Negative answers are kept.** "No such id" is stored as an empty name and
  honoured while fresh; a lookup that FAILED is never stored.
- **A refresh skips the client's read cache** (`use_cache=False`). Once the
  store decides its answer is out of date, a cached copy of the same read is no
  newer; answering from it would save the old answer again as fresh.

The kinds written are the `NAME_LOOKUPS` kinds, `directory` (a person, with
`{"address": ...}` as extra) and `position_budget` (`"Y"`, `"N"` or `""`). A
failing store is treated as an empty one. `MemoryLookupStore` is the in-process
implementation, for tests and single-process hosts.

**Test pods scrub work email.** A non-production pod writes
`sendmail-test-discard@oracle.com` over every `WorkEmail`; `worker_address()`
treats it as absent and falls back to `Username`, which still holds the real
address there.

## The read cache

Reads are cached per a `CachePolicy` over a `Cache`. The default policy caches
the HCM reference data (`REFERENCE_RESOURCES`: grades, departments, business
units, organizations, jobs, positions, job families, workers) for six hours
and nothing else: which of its OWN resources a product caches, and for how
long, is the product's call. `stale_on_write` says which cached resources a
POST or PATCH makes stale at once (a version in the key, bumped on write):

```python
oracle.CachePolicy(
    ttls={**oracle.reference_ttls(), "absences": 120, "absencesLOV": 120},
    stale_on_write={"absences": ("absences", "absencesLOV")},
)
```

An EMPTY collection is kept for ten minutes at most, whatever its resource's
TTL, so a new hire does not read as nobody until tomorrow.

The default store is `MemoryCache` (per process); `NullCache` turns caching
off. For a store every replica shares, implement the four async methods of the
`Cache` protocol (`get`, `set`, `get_int`, `incr`); values are JSON-shaped
dicts. Keys are scoped to the instance URL, so a test pod and production can
share one store.

`get(..., use_cache=False)` (and on `get_collection` / `iter_collection`) goes
to Oracle whatever the cache holds and stores nothing: use it from any layer
above the client that has decided its own copy is out of date.

## Paging limits and attachments

Fusion's own limits, on any resource; both return raw rows.

- `capped_page(client, path, limit=25, offset=0, params=None, page_cap=200,
  offset_ceiling=None)`: Fusion REFUSES a page over its cap, and on resources
  with an offset ceiling (10,000, `DEFAULT_OFFSET_CEILING`) any offset at or
  past it, and the WHOLE request when `offset + limit` crosses it. This
  clamps, trims the last reachable page, answers past the ceiling without
  asking, and flags `at_ceiling` (Fusion reports `hasMore=false` there, which
  does not mean the last row). Past the ceiling, slice the collection with `q`.
- `attachments(client, record_path, category=None)` lists a record's files
  (`/workers/<id>`, `/documentRecords/<id>`, any resource with a
  `child/attachments`), keeping the `links`: the enclosure key lives only there.
  `enclosure_key(row)` extracts it; `download_enclosure(client, record_path,
  key)` reads the bytes. The rows carry a signed `FileUrl`: never forward it to
  a browser.

## Errors

```
OracleError
├── OracleConfigError          the host wired it wrong (raised at construction)
├── OracleQueryError           a value the q grammar cannot carry (also a ValueError)
├── OracleNotConfiguredError   no base URL: Oracle is off here
└── OracleUpstreamError        refused, unreachable, or not JSON (.status, .method, .path,
    │                          .is_transient, .retry_after_seconds on a 429/503)
    ├── OracleNotFoundError        404: a real answer about a real record
    ├── OracleAuthError            401/403, or no token could be had: configuration, never transient
    ├── OracleAlreadyExistsError   a create refused because a caller-minted key is taken
    └── OracleUnavailableError     a breaker is open: refused at once (.retry_after_seconds)
```

Oracle's response body is never put on an exception (it carries tenant detail
and signed URLs); it is logged at debug level. None of these carries an HTTP
status for *your* callers; mapping them onto your API is host policy.

## Testing a host

Pass `http=httpx.AsyncClient(transport=httpx.MockTransport(handler))` and,
usually, `cache=NullCache()`; nothing leaves the process. The package's own
`tests/conftest.py` has a routing fake worth copying.

See the repo README for the family contract. Extracted from a product's
working Fusion integration and generalised; nothing product-specific stayed.
