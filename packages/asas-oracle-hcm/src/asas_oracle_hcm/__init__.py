"""Asas Oracle Fusion HCM: an async REST client for Oracle Fusion, with the
instance's traps written down once.

Domain-agnostic: it knows Fusion (the query grammar, the media types, the
limits, the HCM reference data every product resolves ids against) and nothing
about what a product does with it. Recruiting, core HR, absence or payroll
integrations all build on the same client; which resources a product reads,
what a row means and which of them it caches are the product's.

Public surface: a **table-less, router-less** package that fills none of the
four host-contract slots. Everything is an object the host constructs:

- :class:`OracleSettings`: base URL, Basic credentials, timeout, optional
  gateway API key. An empty base URL means "Oracle is off here".
- :class:`OracleFusionClient`: ``get`` / ``get_collection`` /
  ``iter_collection`` / ``get_bytes`` / ``post`` / ``patch``, read-through
  cached per a :class:`CachePolicy` over a :class:`Cache`
  (:class:`MemoryCache` by default, :class:`NullCache` to turn it off).
- :class:`OracleLookups`: id-to-name lookups (:data:`NAME_LOOKUPS`), people by
  person id, positions with their budget flag, a worker by email address and
  their department, and a reference set's departments. Optionally backed by a
  :class:`LookupStore` shared across processes (stale answers served and
  refreshed behind the caller, negative answers kept);
  :class:`MemoryLookupStore` is the in-process one.
- Upstream health: a :class:`Breaker` that makes reads fail fast with
  :class:`OracleUnavailableError` during an outage, per-resource call
  statistics (``client.health.snapshot()``), and :func:`count_calls` to count
  the Oracle requests one host request made.
- :mod:`asas_oracle_hcm.check` (``asas-oracle-check``): each read a deployment
  depends on called once, from a manifest of :class:`Probe` s, for verifying a
  gateway registration (the HCM reference reads by default).
- ``query``: the ``q`` grammar (:func:`eq`, :func:`like`, :func:`and_`,
  :func:`literal`) and row readers (:func:`text`, :func:`flag`,
  :func:`integer`, :func:`child_items`).
- Fusion's paging limits and attachments, on any resource:
  :func:`capped_page` (the page cap and the offset ceiling),
  :func:`attachments`, :func:`enclosure_key`, :func:`download_enclosure`.
- Errors: :class:`OracleError` > :class:`OracleConfigError`,
  :class:`OracleNotConfiguredError`, :class:`OracleUpstreamError` >
  :class:`OracleNotFoundError`, :class:`OracleAlreadyExistsError`,
  :class:`OracleUnavailableError`.
"""

from __future__ import annotations

from .attachments import attachments, download_enclosure, enclosure_key
from .cache import (
    LOOKUP_TTL_SECONDS,
    REFERENCE_RESOURCES,
    Cache,
    CachePolicy,
    MemoryCache,
    NullCache,
    reference_ttls,
)
from .check import REFERENCE_PROBES, CheckResult, Probe, check, load_manifest
from .client import MAX_PAGE_SIZE, CollectionPage, OracleFusionClient
from .errors import (
    OracleAlreadyExistsError,
    OracleConfigError,
    OracleError,
    OracleNotConfiguredError,
    OracleNotFoundError,
    OracleUnavailableError,
    OracleUpstreamError,
)
from .lookups import (
    DEFAULT_CONCURRENCY,
    DIRECTORY_KIND,
    NAME_LOOKUPS,
    POSITION_BUDGET_KIND,
    SCRUBBED_WORK_EMAIL,
    OracleLookups,
    Person,
    Position,
    WorkerDepartment,
    worker_address,
)
from .query import and_, child_items, eq, flag, integer, like, literal, text
from .paging import DEFAULT_OFFSET_CEILING, CappedPage, capped_page
from .settings import OracleSettings
from .store import LookupStore, MemoryLookupStore, StoredAnswer
from .upstream import Breaker, CallCount, UpstreamHealth, count_calls

__version__ = "0.1.0"

__all__ = [
    "Breaker",
    "Cache",
    "CachePolicy",
    "CallCount",
    "CappedPage",
    "CheckResult",
    "CollectionPage",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_OFFSET_CEILING",
    "DIRECTORY_KIND",
    "LOOKUP_TTL_SECONDS",
    "LookupStore",
    "MAX_PAGE_SIZE",
    "MemoryCache",
    "MemoryLookupStore",
    "NAME_LOOKUPS",
    "NullCache",
    "OracleAlreadyExistsError",
    "OracleConfigError",
    "OracleError",
    "OracleFusionClient",
    "OracleLookups",
    "OracleNotConfiguredError",
    "OracleNotFoundError",
    "OracleSettings",
    "OracleUnavailableError",
    "OracleUpstreamError",
    "POSITION_BUDGET_KIND",
    "Person",
    "Position",
    "Probe",
    "REFERENCE_PROBES",
    "REFERENCE_RESOURCES",
    "SCRUBBED_WORK_EMAIL",
    "StoredAnswer",
    "UpstreamHealth",
    "WorkerDepartment",
    "and_",
    "attachments",
    "capped_page",
    "check",
    "child_items",
    "count_calls",
    "download_enclosure",
    "enclosure_key",
    "eq",
    "flag",
    "integer",
    "like",
    "literal",
    "load_manifest",
    "reference_ttls",
    "text",
    "worker_address",
    "__version__",
]
