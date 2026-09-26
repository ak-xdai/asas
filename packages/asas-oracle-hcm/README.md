# asas-oracle-hcm

Oracle Fusion HCM over REST, async: a client with typed errors, the query
language's traps written down once, a read cache behind a seam, and id-to-name
lookups shaped to what a real Fusion pod will actually answer. Every product
that integrates Fusion rediscovers the same things (there is no working OR,
several ids in one query return 500, test pods scrub every work email, a
duplicate key comes back as a 400 with prose); they are solved here.

Nothing in it is about any one product. It is a **table-less, router-less**
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
    "/recruitingJobRequisitions",
    {"q": oracle.and_(oracle.eq("BusinessUnitId", bu, quote=False),
                      oracle.like("Title", "architect")),
     "limit": 25, "totalResults": "true"},
)
page.items, page.has_more, page.total          # total is None when Oracle declines to count

names = await lookups.names("grade", [row["GradeId"] for row in page.items])
people = await lookups.people([row["HiringManagerId"] for row in page.items])
where = await lookups.worker_department("jane@example.gov")   # WorkerDepartment | None

# shutdown
await client.aclose()        # or: async with oracle.OracleFusionClient(...) as client:
```

## Settings

`OracleSettings(base_url, username, password, timeout_seconds=30,
gateway_api_key="", gateway_api_key_header="x-api-key")`. It validates at
construction, so a half-wired host fails at boot.

An **empty `base_url` is allowed** and means "Oracle is off in this
deployment": `client.configured` is `False` and the first call raises
`OracleNotConfiguredError`, so a host can boot, and a status page can answer,
without an instance.

`gateway_api_key` is for a deployment that reaches Fusion through an API
gateway: every request also carries the key in `gateway_api_key_header`. The
Basic credentials still travel, so the key is an addition, never a replacement,
and an unset key sends no header. `OracleSettings.from_env(prefix="ORACLE_HCM_")`
reads `BASE_URL`, `USERNAME`, `PASSWORD` and the optional `TIMEOUT_SECONDS`,
`GATEWAY_API_KEY`, `GATEWAY_API_KEY_HEADER`.

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
  phase and state NAMES on a requisition, because `fields=...StateId` drops
  them and leaves ids nothing resolves.

**No retries.** Whether and when to retry is host policy. A PATCH by id is
idempotent and safe for a caller to retry; a transition such as
`POST .../action/move` is not, because a repeat advances the record again.

A host that needs a private CA, a proxy or a shared pool passes its own
`httpx.AsyncClient` as `http=`; the library uses it as-is and does not close it.

## The query language

What a real pod does with `q`, and the helpers that keep you off the traps:

- **AND is `;`** (`and_(...)`). The word `AND` returns an empty body.
- **There is no working OR.** `OR`, `IN (...)` and a comma list all silently
  match nothing. A facet takes one value; several ids mean several requests.
- `like(field, term)` is `field LIKE '%term%'`, case-insensitive on `Title`.
- **Values are single-quoted with no escape form**, so `literal()` strips
  quotes. `eq(field, value)` quotes; `eq(..., quote=False)` leaves a bare
  numeric id, the form the id lookups use.
- **Equality is case-sensitive.**

Row readers: `text` (null reads as ""), `flag` (JSON booleans and "Y"/"N"
alike), `integer`, and `child_items` for an expanded child, which arrives as a
bare list on some resources and as `{"items": [...]}` on others.

## Lookups

`OracleLookups(client, concurrency=12, name_ttl_seconds=21600,
people_ttl_seconds=3600)`. Hold one per process.

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
- `find_worker(address, expand=None)` and `worker_department(address)`: a
  worker by email, by `WorkEmail` then `Username`, each as given and
  lowercased, and the department on their primary assignment.
- `departments_in_set(set_code, active_only=True)`: `/departments` carries no
  business unit and is partitioned by reference set (`SetCode`).

Lookups are fail-soft: a failed or unknown id is absent from the answer, and a
failure costs a name, never the request. `departments_in_set` is the exception,
because a caller syncing a catalogue must know it got the whole list.

**Test pods scrub work email.** A non-production pod writes
`sendmail-test-discard@oracle.com` over every `WorkEmail`; `worker_address()`
treats it as absent and falls back to `Username`, which still holds the real
address there.

## The read cache

Reads are cached per a `CachePolicy` over a `Cache`. The default policy caches
reference data (grades, departments, business units, organizations, jobs,
positions, job families, workers) for six hours, the recruiting geography for
fifteen minutes and requisitions for two, and never caches candidates,
applications or attachments. A POST or PATCH on a requisition makes cached
requisition reads stale at once (a version in the key, bumped on write). An
EMPTY collection is kept for ten minutes at most, so a new hire does not read
as nobody until tomorrow.

The default store is `MemoryCache` (per process); `NullCache` turns caching
off. For a store every replica shares, implement the four async methods of the
`Cache` protocol (`get`, `set`, `get_int`, `incr`); values are JSON-shaped
dicts. Keys are scoped to the instance URL, so a test pod and production can
share one store.

## Recruiting helpers

Oracle's own limits, not any product's; they return raw rows.

- `candidate_page(client, limit, offset, q=None)`: `/recruitingCandidates`
  refuses a page over 200 and any offset at or past 10,000, and refuses the
  WHOLE request when `offset + limit` crosses the ceiling. This clamps, trims
  the last page and flags `at_ceiling` (Oracle reports `hasMore=false` there,
  which does not mean the last candidate).
- `candidate_attachments(client, number, category=None)` keeps the `links`
  (the enclosure key lives only there); `enclosure_key(row)` extracts it;
  `download_attachment(client, number, key)` reads the file. The rows carry a
  signed `FileUrl`: never forward it to a browser.

## Errors

```
OracleError
├── OracleConfigError          the host wired it wrong (raised at construction)
├── OracleNotConfiguredError   no base URL: Oracle is off here
└── OracleUpstreamError        refused, unreachable, or not JSON (.status, .method, .path, .is_transient)
    ├── OracleNotFoundError        404: a real answer about a real record
    └── OracleAlreadyExistsError   a create refused because a caller-minted key is taken
```

Oracle's response body is never put on an exception (it carries tenant detail
and signed URLs); it is logged at debug level. None of these carries an HTTP
status for *your* callers; mapping them onto your API is host policy.

## Testing a host

Pass `http=httpx.AsyncClient(transport=httpx.MockTransport(handler))` and,
usually, `cache=NullCache()`; nothing leaves the process. The package's own
`tests/conftest.py` has a routing fake worth copying.

See the repo README for the family contract. Extracted from the AI Recruiter's
`oracle_hcm` module.
