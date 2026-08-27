"""Asas search — cross-entity search engine with an optional deep-content tier.

Extracted from Teamy (search epic WXL-44, phases 1–3; extraction epic TEAMY-466,
design record 0017 — full search design in Teamy's decision record 0006). The
package never imports host models:

- ``engine`` — the provider registry and composition: the host registers one or
  more *providers* per searchable entity type; :func:`search` merges, dedupes
  (strongest signal per record), rank-tiers, and truncates each group.
  Visibility is the provider's job; providers must match only fields every
  viewer of the record may read.
- ``fts`` — the Postgres-tier deep-content index (``search_document``: bilingual
  english+arabic tsvector, raw SQL, dialect-guarded). The host supplies
  *extractors* (rows → :class:`fts.IndexDoc`) and *resolvers* (matched entity
  ids → visible titles/urls) and keeps the index fresh via its own ORM events;
  :func:`fts.make_provider` fuses lexical FTS with semantic KNN via RRF.
- ``semantic`` / ``embeddings`` — pgvector KNN over the same rows, filled
  after-commit by :func:`semantic.embed_pending`; one OpenAI-compatible adapter
  (base_url selects OpenAI/Azure/local). Unconfigured ⇒ pure-lexical, nothing
  breaks.

Host contract (table-owning variant): :func:`migrate` applies the package
Alembic chain — **dialect-branched**: the ``search_document`` DDL runs on
Postgres only; on SQLite the chain records its version and creates nothing
(the deep tier simply stays off, matching the engine's graceful degradation).
"""

from . import embeddings, fts, semantic
from .engine import (
    TIER_BASE_FIELD,
    TIER_CONTENT,
    TIER_RELATED,
    TIER_TITLE_CONTAINS,
    TIER_TITLE_PREFIX,
    SearchHit,
    register_provider,
    registered_types,
    search,
    title_tier,
)
from .migrate import migrate

__version__ = "0.12.0"

__all__ = [
    "TIER_BASE_FIELD",
    "TIER_CONTENT",
    "TIER_RELATED",
    "TIER_TITLE_CONTAINS",
    "TIER_TITLE_PREFIX",
    "SearchHit",
    "embeddings",
    "fts",
    "migrate",
    "semantic",
    "register_provider",
    "registered_types",
    "search",
    "title_tier",
    "__version__",
]
