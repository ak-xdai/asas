"""The mandatory access layer (TEAMY-59, DR 0021): clearance levels + markings.

Where the rest of this package is **discretionary** (grant rows *extend* access,
union-only, admin floor), this module is **mandatory**: a record's classification
*restricts* who can see it regardless of any grant — including admins (the owner
ruling that makes need-to-know real; break-glass is the host's audited
read-yourself-in path, never a silent bypass here).

Evaluation order is MAC-then-DAC: :func:`mac_allows` runs at the top of
``visibility.can_view_record``, so a caller failing MAC never reaches the
discretionary logic — the record is simply invisible.

Semantics:

* A record with no ``classification_code`` and no ``record_marking`` rows is
  **unclassified** — this module always allows it (the layer is dormant until a
  host stamps something).
* Classification: subject clearance rank ≥ record level rank. A subject with no
  assigned clearance holds the org catalog's **minimum** rank implicitly (so
  stamping a record at the lowest level is a harmless no-op). A stamp whose code
  is missing from the catalog (deleted level) **fails closed**.
* Markings: the record's markings must be a **subset** of the subject's (AND
  semantics — compartments compose restrictively, unlike groups' union).
* ``None`` user (anonymous) never sees classified content.

Like the principal layer, the subject side is host-resolved: the engine owns the
``subject_clearance`` / ``subject_marking`` tables keyed by ``org_membership_id``,
but the join from a request user to their membership lives host-side — the host
registers a *subject source* (the ``_group_keys`` pattern) and should session-cache
it. A missing or raising source yields an empty profile, which **denies** classified
content — fail-closed, never fail-open.

Framework-agnostic like the rest of the package: booleans only, nothing HTTP,
enforcement on/off is the caller's concern.
"""

from typing import Any, Callable, Dict, Optional, Set, Tuple

from sqlalchemy import event
from sqlmodel import Session, select

from .models import ClearanceLevel, RecordMarking

# Entity types opted into MAC checks (host-registered). Unregistered types skip
# the layer entirely — no queries, always allowed. Child entities (work items,
# attachments, …) need no registration: their host read paths already resolve
# and check the stamped parent record (inheritance-at-check-time, ruling 4).
_CLASSIFIED: Set[str] = set()

# fn(user, session) -> (level_code | None, set of marking codes) — the host-side
# join from a request user to their subject_clearance/subject_marking rows.
SubjectSource = Callable[[Any, Any], Tuple[Optional[str], Set[str]]]
_subject_source: Optional[SubjectSource] = None

# {org_id: {code: rank}} — the level catalogs, process-cached like the grant
# caches (small, read-hot, invalidated on catalog writes).
_level_cache: Optional[Dict[int, Dict[str, int]]] = None


def register_classified_entity(entity_type: str) -> None:
    """Opt ``entity_type`` into MAC checks — its records carry a host-owned
    ``classification_code`` column and may carry ``record_marking`` rows."""
    _CLASSIFIED.add(entity_type)


def classified_entities() -> Set[str]:
    return set(_CLASSIFIED)


def register_subject_source(fn: SubjectSource) -> None:
    """Register how a request user resolves to their (clearance level, markings)
    profile. One source — re-registering replaces (idempotent at boot)."""
    global _subject_source
    _subject_source = fn


def invalidate_mac_cache() -> None:
    """Drop the level-catalog cache (call after catalog writes/seeds)."""
    global _level_cache
    _level_cache = None


def _levels(session: Session) -> Dict[int, Dict[str, int]]:
    global _level_cache
    if _level_cache is None:
        cache: Dict[int, Dict[str, int]] = {}
        for row in session.exec(select(ClearanceLevel)).all():
            cache.setdefault(row.org_id, {})[row.code] = row.rank
        _level_cache = cache
    return _level_cache


def _record_markings_map(
    session: Session, org_id: int, entity_type: str
) -> Dict[int, Set[str]]:
    """All marking stamps for one (org, entity type), cached on the session
    (request-scoped) so list paths cost one query, not one per record."""
    cache = session.info.setdefault("_access_record_markings", {})
    key = (org_id, entity_type)
    if key not in cache:
        loaded: Dict[int, Set[str]] = {}
        for row in session.exec(
            select(RecordMarking).where(
                RecordMarking.org_id == org_id,
                RecordMarking.entity_type == entity_type,
            )
        ).all():
            loaded.setdefault(row.record_id, set()).add(row.marking_code)
        cache[key] = loaded
    return cache[key]


def record_markings(
    session: Session, org_id: int, entity_type: str, record_id: int
) -> Set[str]:
    """The marking codes stamped on one record (empty when unmarked)."""
    return set(_record_markings_map(session, org_id, entity_type).get(record_id, set()))


def invalidate_record_markings(session: Session) -> None:
    """Drop the session's record-marking cache (call after stamping/unstamping
    within a request that will re-check)."""
    session.info.pop("_access_record_markings", None)


def subject_access_profile(
    session: Session, user: Any
) -> Tuple[Optional[str], Set[str]]:
    """The (clearance level code, marking codes) profile of ``user`` via the
    host-registered source. Anonymous, missing source, or a raising source ⇒
    the empty profile — which *denies* classified content (fail-closed)."""
    if user is None or _subject_source is None:
        return (None, set())
    try:
        level, markings = _subject_source(user, session)
        return (level, set(markings))
    except Exception:
        return (None, set())


def _classification_of(record: Any) -> Optional[str]:
    v = getattr(record, "classification_code", None)
    return getattr(v, "value", v)  # Enum -> value; plain str/None passes through


def mac_allows(
    session: Session,
    user: Any,
    entity_type: str,
    record: Any,
    *,
    record_org_id: Optional[int] = None,
) -> bool:
    """Whether the mandatory layer permits ``user`` to see ``record``. True for
    unregistered entity types and unclassified records; otherwise requires
    clearance rank ≥ the record's level rank AND record markings ⊆ subject
    markings. **No admin floor** — that is the point of this layer.

    The org the security decision runs under is the RECORD's — from the record
    itself or the explicit ``record_org_id`` — never the caller's (DR 0001
    T6/D4, audit defect T-3): substituting the caller's org looked up another
    tenant's markings and rank catalog, reading a marked record as
    unclassified and failing OPEN. A stamped record whose org cannot be
    resolved denies."""
    if record is None or entity_type not in _CLASSIFIED:
        return True
    org_id = getattr(record, "org_id", None)
    if org_id is None:
        org_id = record_org_id
    classification = _classification_of(record)
    marks: Set[str] = set()
    record_id = getattr(record, "id", None)
    if record_id is not None:
        if org_id is not None:
            marks = record_markings(session, org_id, entity_type, record_id)
        else:
            # Org unresolvable (record carries no org_id and the caller has
            # none — e.g. an anonymous/system subject): resolve the stamps
            # directly, uncached. Record ids are unique per entity table, so
            # the org filter is redundant for a point lookup. Skipping this
            # lookup would FAIL OPEN for marked-only records — never do that.
            marks = {
                row.marking_code
                for row in session.exec(
                    select(RecordMarking).where(
                        RecordMarking.entity_type == entity_type,
                        RecordMarking.record_id == record_id,
                    )
                ).all()
            }
    if classification is None and not marks:
        return True  # unclassified — the layer is dormant for this record
    if user is None:
        return False
    level, held_marks = subject_access_profile(session, user)
    if marks and not marks <= held_marks:
        return False
    if classification is not None:
        if org_id is None:
            return False  # stamped record with no resolvable org — fail closed
        ranks = _levels(session).get(org_id, {})
        record_rank = ranks.get(classification)
        if record_rank is None:
            return False  # stamp references a deleted level — fail closed
        if level is not None:
            subject_rank = ranks.get(level)
            if subject_rank is None:
                return False  # assignment references a deleted level
        else:
            # Unassigned subjects hold the catalog's minimum rank implicitly.
            subject_rank = min(ranks.values())
        if subject_rank < record_rank:
            return False
    return True


def ensure_clearance_levels(
    session: Session, org_id: int, levels: Dict[str, Tuple[str, int]]
) -> None:
    """Idempotently create the host's default level catalog (``code -> (name,
    rank)``) for one org. Presence-idempotent: existing rows are never updated
    (org edits win). It cannot remember deletions — hosts gate re-application
    with a stamp (the DR 0020 bootstrap pattern) so an org's deleted levels
    don't resurrect at boot."""
    existing = {
        row.code
        for row in session.exec(
            select(ClearanceLevel).where(ClearanceLevel.org_id == org_id)
        ).all()
    }
    added = False
    for code, (name, rank) in levels.items():
        if code not in existing:
            session.add(
                ClearanceLevel(org_id=org_id, code=code, name=name, rank=rank)
            )
            added = True
    if added:
        # Invalidate only once the rows are durable (DR 0001 T8, audit defect
        # T-9): clearing the process-global cache at mutation time lets any
        # other session re-fill it from state a rollback then erases —
        # phantom levels served until the next invalidation.
        event.listen(session, "after_commit", lambda s: invalidate_mac_cache(), once=True)
