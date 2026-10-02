# Changelog: `asas-oracle-hcm`

Versions follow semver, and the git tag matches this file: `asas-oracle-hcm/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a product's working Oracle Fusion integration and
generalised to be **domain-agnostic**: it knows Fusion (the query grammar, the
media types, the paging limits, attachments, the HCM reference data) and
nothing about recruiting or any other product. What a resource means, which
resources a product reads and which it caches stay with the product.

- **`OracleSettings`** replaces the product's settings import: an explicit
  object validated at construction, where an empty base URL means "Oracle is
  off" rather than an error, plus the optional gateway API key header.
- **`OracleFusionClient`** is a plain object the host owns (or hands its own
  `httpx.AsyncClient`), with `get`, `get_collection` (a `CollectionPage`, `-1`
  totals read as `None`), `iter_collection`, `get_bytes`, `post` and `patch`,
  each with the media type Oracle insists on. Errors are a typed hierarchy with
  no HTTP-status baggage and never carry Oracle's body.
- **The read cache is a seam.** A four-method `Cache` protocol with
  `MemoryCache` (default) and `NullCache`, and a `CachePolicy` saying which
  resources are cached, for how long, and which writes make them stale. The
  default caches the HCM reference data alone (`reference_ttls()`); a product
  adds its own resources.
- **`OracleLookups`** holds what were module-level caches as instance state:
  id-to-name lookups for eight kinds, people by person id, a worker and their
  department by email address, and a reference set's departments.
- The `q` grammar helpers and row readers, with the instance's traps (no OR,
  `;` for AND, no quote escaping, case-sensitive equality) documented.
- Fusion's paging limits and attachments on any resource: `capped_page` (the
  page cap and the offset ceiling, `at_ceiling` where `hasMore=false` lies),
  `attachments` (with the `links` the enclosure key lives in),
  `enclosure_key`, `download_enclosure`.
- **Upstream health.** A per-client `Breaker` makes reads fail at once with
  `OracleUnavailableError` after consecutive faults (transport, 5xx, 429),
  then lets one probe decide; writes are counted, never refused.
  `client.health.snapshot()` carries the breaker and per-resource call
  statistics; `count_calls()` counts the Oracle requests one host request made.
- **Gateway-only authentication.** Basic auth is sent only when a username is
  set, so a gateway that authenticates to the integration layer itself takes
  the API key alone; a `base_url` needs credentials, a key, or both.
- **A bounded, warm connection pool** (`max_connections`) and retries for a
  connection that could not be opened (`connect_retries`), on the client the
  library owns.
- **`use_cache=False`** on `get`, `get_collection` and `iter_collection`.
- **`LookupStore`**, an optional persistent home for lookup answers shared by
  every process: stale answers served and refreshed in the background,
  negative answers kept, failures never stored, refreshes that skip the read
  cache. `MemoryLookupStore` is the in-process implementation.
- **`OracleLookups.positions()`**: a position's name and budget flag in one
  request.
- **`asas-oracle-check`** (`check(client, probes)`): each read in a manifest
  called once, to verify a gateway registration; values chain between probes,
  writes are listed and never called, no body is printed. The default manifest
  is the HCM reference reads; `examples/recruiting-probes.json` is a recruiting
  one.
- **Credentials are a seam** (`Auth`): `BasicAuth`, `ApiKeyAuth`,
  `BearerToken`, `OAuthClientCredentials` (a client-credentials token, cached,
  refreshed before expiry and renewed after a 401 with one retry) and
  `CompositeAuth`, built by `OracleSettings.auth()` from the settings
  (`oauth_*` fields), or passed as `auth=`. Basic and OAuth are refused
  together; no credential ever reaches a log or an exception.
- **Breakers per resource plus one for the host** (`BreakerPolicy`), counting
  only real outages (`is_outage`: no response, 502/503/504, 429). A 500 is an
  answer, so one broken operation cannot switch every read off.
- **`OracleAuthError`** for 401/403 (never transient, never an outage) and
  **`retry_after_seconds`** on a 429/503.
- **`OracleQueryError`**: `eq`, `like` and `literal` refuse a quote or a `;`
  instead of guessing (an unquoted value must be a plain id; field names must
  be attribute names), so caller input cannot widen a query.
  `strip_quotes=True` is the explicit opt-in for free text.
- **`client.request(...)`** is public, with `raise_for_status` and `guard`;
  the gateway check uses it instead of the client's internals.
- **`OracleLookups(kinds=...)`** adds a host's own id kinds; **`extra_headers`**
  ride on every request; an **`on_request`** hook sees every request
  (`RequestEvent`).
- Depends on `httpx` only.
