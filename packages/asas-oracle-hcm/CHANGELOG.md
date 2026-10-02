# Changelog: `asas-oracle-hcm`

Versions follow semver, and the git tag matches this file: `asas-oracle-hcm/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from the AI Recruiter's `oracle_hcm` module
(`OracleHcmClient` and the lookup functions in its service) and generalised;
every recruiting rule (what "approved" means, the row mappers, the requisition
import and sync) stayed behind.

- **`OracleSettings`** replaces the product's settings import: an explicit
  object validated at construction, where an empty base URL means "Oracle is
  off" rather than an error, plus the optional gateway API key header.
- **`OracleFusionClient`** is a plain object the host owns (or hands its own
  `httpx.AsyncClient`), with `get`, `get_collection` (a `CollectionPage`, `-1`
  totals read as `None`), `iter_collection`, `get_bytes`, `post` and `patch`,
  each with the media type Oracle insists on. Errors are a typed hierarchy with
  no HTTP-status baggage and never carry Oracle's body.
- **The read cache is a seam.** The product imported its own Redis module; here
  a four-method `Cache` protocol with `MemoryCache` (default) and `NullCache`,
  and a `CachePolicy` saying which resources are cached, for how long, and
  which writes make them stale.
- **`OracleLookups`** holds what were module-level caches as instance state:
  id-to-name lookups for eight kinds, people by person id, a worker and their
  department by email address, and a reference set's departments.
- The `q` grammar helpers and row readers, with the instance's traps (no OR,
  `;` for AND, no quote escaping, case-sensitive equality) documented.
- Recruiting helpers that respect Oracle's own caps: `candidate_page` (200 per
  page, 10,000 offset ceiling), `candidate_attachments`, `enclosure_key`,
  `download_attachment`.
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
- **`asas-oracle-check`** (`asas_oracle_hcm.check`): every recruiting read
  called once, to verify a gateway registration. Never writes, never prints a
  body.
- Depends on `httpx` only.
