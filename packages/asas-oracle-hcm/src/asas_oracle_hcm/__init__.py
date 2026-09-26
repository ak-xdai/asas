"""Asas Oracle Fusion HCM: an async REST client for Oracle Fusion, with the
instance's traps written down once.

Extracted from a product's working integration (the AI Recruiter's
``oracle_hcm`` module) and generalised: settings are an explicit object, the
read cache is a seam with an in-memory default instead of a Redis import, and
every recruiting rule (what "approved" means, how a row maps onto the
product's schema) stayed behind.

Public surface: a **table-less, router-less** package that fills none of the
four host-contract slots. Everything is an object the host constructs:

- :class:`OracleSettings`: base URL, Basic credentials, timeout, optional
  gateway API key. An empty base URL means "Oracle is off here".
- :class:`OracleFusionClient`: ``get`` / ``get_collection`` /
  ``iter_collection`` / ``get_bytes`` / ``post`` / ``patch``, read-through
  cached per a :class:`CachePolicy` over a :class:`Cache`
  (:class:`MemoryCache` by default, :class:`NullCache` to turn it off).
- :class:`OracleLookups`: id-to-name lookups (:data:`NAME_LOOKUPS`), people by
  person id, a worker by email address and their department, and a reference
  set's departments.
- ``query``: the ``q`` grammar (:func:`eq`, :func:`like`, :func:`and_`,
  :func:`literal`) and row readers (:func:`text`, :func:`flag`,
  :func:`integer`, :func:`child_items`).
- Recruiting helpers that respect Oracle's own caps: :func:`candidate_page`,
  :func:`candidate_attachments`, :func:`enclosure_key`,
  :func:`download_attachment`.
- Errors: :class:`OracleError` > :class:`OracleConfigError`,
  :class:`OracleNotConfiguredError`, :class:`OracleUpstreamError` >
  :class:`OracleNotFoundError`, :class:`OracleAlreadyExistsError`.
"""

from __future__ import annotations

from .cache import Cache, CachePolicy, MemoryCache, NullCache
from .client import MAX_PAGE_SIZE, CollectionPage, OracleFusionClient
from .errors import (
    OracleAlreadyExistsError,
    OracleConfigError,
    OracleError,
    OracleNotConfiguredError,
    OracleNotFoundError,
    OracleUpstreamError,
)
from .lookups import (
    DEFAULT_CONCURRENCY,
    NAME_LOOKUPS,
    SCRUBBED_WORK_EMAIL,
    OracleLookups,
    Person,
    WorkerDepartment,
    worker_address,
)
from .query import and_, child_items, eq, flag, integer, like, literal, text
from .recruiting import (
    CANDIDATE_MAX_PAGE_SIZE,
    CANDIDATE_OFFSET_CEILING,
    CandidatePage,
    candidate_attachments,
    candidate_page,
    download_attachment,
    enclosure_key,
)
from .settings import OracleSettings

__version__ = "0.1.0"

__all__ = [
    "CANDIDATE_MAX_PAGE_SIZE",
    "CANDIDATE_OFFSET_CEILING",
    "Cache",
    "CachePolicy",
    "CandidatePage",
    "CollectionPage",
    "DEFAULT_CONCURRENCY",
    "MAX_PAGE_SIZE",
    "MemoryCache",
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
    "OracleUpstreamError",
    "Person",
    "SCRUBBED_WORK_EMAIL",
    "WorkerDepartment",
    "and_",
    "candidate_attachments",
    "candidate_page",
    "child_items",
    "download_attachment",
    "enclosure_key",
    "eq",
    "flag",
    "integer",
    "like",
    "literal",
    "text",
    "worker_address",
    "__version__",
]
