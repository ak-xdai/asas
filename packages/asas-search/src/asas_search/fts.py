"""Postgres deep-content search (Phase 2, WXL-150; design in Teamy DR 0006).

Ranked full-text search over long prose (experience descriptions, CV text, project
registers) via one side table, ``search_document``:

- **Postgres-tier only.** The table is created by a hand-written, dialect-branched
  Alembic migration (no-op on SQLite); ``alembic/env.py`` excludes it from
  autogenerate. Every function here is raw SQL and must only run against Postgres —
  callers guard with :func:`is_postgres`.
- **Self-contained.** Like the rest of the package, this module never imports app
  models. App code (``search_wiring``) supplies *extractors* (how to turn its rows
  into :class:`IndexDoc`) and *resolvers* (how to turn matched entity ids back into
  titles/urls, applying visibility), and keeps the index fresh via ORM events.
- **Never index restricted fields**: the index is a write-time copy, so redaction
  cannot be applied at query time — extractors must only feed text every viewer of
  the record may read.
- Bilingual: the tsvector combines the ``english`` and ``arabic`` configurations, and
  queries OR both interpretations, so either language matches with stemming.
"""

from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from sqlalchemy import text

from . import semantic
from .engine import TIER_CONTENT, Provider, SearchHit


class IndexDoc(NamedTuple):
    entity_type: str  # owning record the hit navigates to ("member" | "project")
    entity_id: int
    source: str  # corpus name, e.g. "experience", "cv", "risk"
    source_id: int  # source row id — the upsert/delete key
    content: str  # the indexed prose
    context: str  # human label shown with the snippet, e.g. "via risk: Outage"
    # Owning org (WXL-238): a write-time copy like the content itself — extractors
    # fill it from the owning entity; queries filter on the caller's org.
    org_id: Optional[int] = None


# source -> extractor yielding every IndexDoc of that corpus (used by rebuild()).
_EXTRACTORS: Dict[str, Callable[[Any], Iterable[IndexDoc]]] = {}

# (title, subtitle, url_path) per visible entity id; ids the viewer may not see are
# simply omitted by the resolver.
Resolver = Callable[[Any, Any, List[int]], Dict[int, Tuple[str, Optional[str], str]]]


def register_extractor(source: str, fn: Callable[[Any], Iterable[IndexDoc]]) -> None:
    """Register how one source turns rows into :class:`IndexDoc` s.

    ``fn`` receives the **session** — not a record — and yields every document
    for this source. It runs on :func:`rebuild`.

    **Registering an extractor does not keep the index fresh.** Nothing calls it
    outside a rebuild, so a host needs all three of: this registration, a
    backfill (``rebuild``) for rows that already exist, and ORM event listeners
    calling :func:`upsert`/:func:`delete` for rows written afterwards. With only
    the first, the index stays empty for ever and every *negative* search
    assertion still passes — which is how an inert deep tier looks tested.
    """
    _EXTRACTORS[source] = fn


def is_postgres(bind: Any) -> bool:
    return bind.dialect.name == "postgresql"


# tsv is computed on write (not a generated column) so the expression can evolve
# without DDL; rebuild() re-derives everything.
_INSERT = text(
    """
    INSERT INTO search_document
        (entity_type, entity_id, source, source_id, content, context, org_id, tsv)
    VALUES
        (:entity_type, :entity_id, :source, :source_id, :content, :context, :org_id,
         to_tsvector('english', :content) || to_tsvector('arabic', :content))
    """
)
_DELETE = text("DELETE FROM search_document WHERE source = :source AND source_id = :source_id")

_QUERY = text(
    """
    SELECT DISTINCT ON (entity_id)
           entity_id,
           context,
           source,
           ts_rank(tsv, query) AS score,
           ts_headline('simple', content, query,
                       'MaxWords=16, MinWords=6, MaxFragments=1') AS snippet
    FROM search_document,
         (SELECT websearch_to_tsquery('english', :q)
                 || websearch_to_tsquery('arabic', :q) AS query) params
    WHERE entity_type = :entity_type AND org_id = :org_id AND tsv @@ query
    ORDER BY entity_id, score DESC
    """
)


def upsert(conn: Any, doc: IndexDoc) -> None:
    """Replace the index row for (source, source_id). Empty content = plain delete."""
    conn.execute(_DELETE, {"source": doc.source, "source_id": doc.source_id})
    if (doc.content or "").strip():
        conn.execute(_INSERT, doc._asdict())


def delete(conn: Any, source: str, source_id: int) -> None:
    conn.execute(_DELETE, {"source": source, "source_id": source_id})


def count(session: Any) -> int:
    return session.execute(text("SELECT COUNT(*) FROM search_document")).scalar()


def rebuild(session: Any) -> int:
    """Drop and re-derive the whole index from the registered extractors — the
    one-time backfill and the drift safety net. Returns the number of docs.

    Note the extractor contract this depends on: an extractor is called as
    ``extractor(session)`` and must yield **every** document for its source, not
    one record's worth. A per-record extractor type-checks fine and simply never
    produces anything here — and since a host that also forgets to call
    ``rebuild`` never exercises it, the mistake can sit undetected behind an
    index that is silently always empty.
    """
    session.execute(text("DELETE FROM search_document"))
    total = 0
    for extractor in _EXTRACTORS.values():
        for doc in extractor(session):
            upsert(session, doc)
            total += 1
    return total


# Standard RRF constant: dampens the head so a hit found by BOTH arms outranks a
# top hit found by only one.
_RRF_K = 60


def make_provider(
    entity_type: str,
    resolver: Resolver,
    org_of: Callable[[Any, Any], Optional[int]],
    row_filter: Optional[Callable[[Any, Any, str, int], bool]] = None,
) -> Provider:
    """A deep-content search provider for one entity type: lexical FTS + (when an
    embedding provider is configured, Phase 3) semantic KNN, fused with Reciprocal
    Rank Fusion. Merged with the Phase 1 structured provider by the engine (per-id
    dedup; content hits rank last-tier but carry fusion scores, and snippets when
    the lexical arm matched).

    ``org_of(session, user)`` returns the org whose documents this caller may
    search. **Returning ``None`` is a filter, not the absence of one**: it
    matches only documents written with a ``None`` ``org_id``. A single-tenant
    host that returns ``None`` while its extractors stamp a real ``org_id``
    therefore gets an empty result set on every query, for ever, with no error —
    so return the same value the documents carry, whatever that is.

    ``org_of(session, user)`` supplies the caller's org (WXL-238) — the package
    stays model-free; app wiring reads its tenant context. No org ⇒ no deep hits
    (fail closed)."""

    def _provider(session, user, q, lang, limit) -> List[SearchHit]:
        if not is_postgres(session.get_bind()):
            return []
        org_id = org_of(session, user)
        if org_id is None:
            return []  # no org context — never search across tenants
        pool = max(limit * 2, limit)
        lexical = session.execute(
            _QUERY, {"q": q, "entity_type": entity_type, "org_id": org_id}
        ).all()
        lexical = sorted(lexical, key=lambda r: -r.score)
        semantic_rows = semantic.knn(session, entity_type, q, pool, org_id)
        if row_filter is not None:
            lexical = [
                r for r in lexical if row_filter(session, user, r.source, r.entity_id)
            ]
            semantic_rows = [
                r
                for r in semantic_rows
                if row_filter(session, user, r.source, r.entity_id)
            ]
        lexical = lexical[:pool]

        # RRF merge: score = Σ 1/(k + rank) over the arms that found the entity.
        scores: Dict[int, float] = {}
        # Lexical metadata wins: it carries the ts_headline snippet.
        meta: Dict[int, Tuple[str, Optional[str]]] = {}
        for rank, row in enumerate(lexical):
            scores[row.entity_id] = scores.get(row.entity_id, 0.0) + 1 / (_RRF_K + rank)
            meta.setdefault(row.entity_id, (row.context, row.snippet))
        for rank, row in enumerate(semantic_rows):
            scores[row.entity_id] = scores.get(row.entity_id, 0.0) + 1 / (_RRF_K + rank)
            meta.setdefault(row.entity_id, (row.context, None))

        ranked = sorted(scores, key=lambda eid: -scores[eid])[:pool]
        info = resolver(session, user, ranked)
        hits: List[SearchHit] = []
        for entity_id in ranked:
            if entity_id not in info:  # not visible to this viewer
                continue
            title, subtitle, url_path = info[entity_id]
            context, snippet = meta[entity_id]
            if snippet:
                snippet = snippet.replace("<b>", "").replace("</b>", "")
                match_context = f"{context} — “…{snippet}…”"
            else:  # semantic-only hit: no lexical fragment to quote
                match_context = context
            hits.append(
                SearchHit(
                    entity_type=entity_type,
                    id=entity_id,
                    title=title,
                    subtitle=subtitle,
                    match_context=match_context,
                    url_path=url_path,
                    rank_tier=TIER_CONTENT,
                    rank_score=scores[entity_id],
                )
            )
        return hits

    return _provider
