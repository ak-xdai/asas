"""Seeding the lookup tables through the ORM's own object graph, on SQLite.

Why this sits next to ``seeding.py`` rather than replacing it
-------------------------------------------------------------
``seeding.py`` writes every row by hand: it commits the type, reads the id back,
hands that id to the next insert as a plain integer, flushes to obtain the value's
id, then hands *that* to the translations. It has no choice — it also runs on
Postgres, where the package's own note applies, that "a multi-row VALUES insert
casts to the native enum type name, which the migration-built schema (VARCHAR
columns) doesn't have" — so it takes one flush per row.

This module is SQLite-only, so that constraint is gone and the ORM can do what the
ORM is for. SQLAlchemy's unit of work topologically sorts pending inserts by their
foreign keys, so a graph assembled purely through relationships is written
parent-first without a single id being touched here::

    type_row.values.append(value)
    value.translations.append(LookupTranslation(lang="en", label="Male"))
    value.aliases.append(LookupAlias(alias="M"))
    session.add(type_row)
    session.commit()        # -> lookup_type, lookup_value, translation, alias

One commit per type instead of one per value.

That covers three of the four links in the schema. The fourth it cannot help with,
and the reason is in ``models.py``: ``parent_id`` and ``superseded_by_id`` are
declared as bare foreign-key columns on ``LookupValue`` with **no**
``Relationship``. There is no attribute to attach a parent to and nothing for the
unit of work to sort, so those need a real id — which means a second pass once
every value of the type exists. That pass is also what lets a file name a parent
declared further down its array.

Nothing here creates a database on import. ``migrate()`` is the first call that
touches the file, and it is explicit — as is the CLI's ``--db``, which has no
default, because a default is how a stray invocation leaves a file nobody meant
to make.

    python -m asas_lookups.orm_seeding --db lookups.db
    python -m asas_lookups.orm_seeding --db lookups.db --show
"""

from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine, select

from .data import SEED_FILE, SeedType, SeedValue, load
from .migrate import migrate
from .models import (
    LookupAlias,
    LookupStatus,
    LookupTranslation,
    LookupType,
    LookupValue,
    SortMode,
    TypeScope,
)

#: The library's own reference data: standards-based or near-universal vocabulary.
LIBRARY_SEED = SEED_FILE

_RULE = "─" * 78

#: The four tables the package owns, in the order a reader wants them: the list,
#: its values, their names, their search names.
_TABLES = (LookupType, LookupValue, LookupTranslation, LookupAlias)


def default_seeds() -> Sequence[Path]:
    """What this package ships, and nothing else.

    A host's own vocabulary is passed to ``seed()`` explicitly rather than listed
    here. That is not tidiness: TEAMY-803 removed seventeen product-specific types
    from asas-lookups because a second host was inheriting the first one's words,
    and a default that reached for a host file would put them straight back.

        seeder.seed(*default_seeds(), Path("my_product_seed.json"))
    """
    return (LIBRARY_SEED,)


# ─────────────────────────────────────────────────────────────────── reporting
@dataclass
class SeedReport:
    """What one run did. Returned so a caller can log or assert on it.

    ``values_skipped`` is the number that matters on a second boot: seeding is
    presence-idempotent, so a run that writes nothing should skip everything and
    bump no version.
    """

    types_created: list[str] = field(default_factory=list)
    types_existing: list[str] = field(default_factory=list)
    values_created: int = 0
    values_skipped: int = 0
    pointers_linked: int = 0
    versions_bumped: list[str] = field(default_factory=list)

    @property
    def wrote_nothing(self) -> bool:
        return not (self.values_created or self.pointers_linked)

    def summary(self) -> str:
        return (
            f"{len(self.types_created)} types created, "
            f"{len(self.types_existing)} already present, "
            f"{self.values_created} values written, "
            f"{self.values_skipped} skipped, "
            f"{self.pointers_linked} pointers linked"
        )


@dataclass(frozen=True)
class ValueView:
    """One value flattened for display: its own columns plus its children."""

    row: LookupValue
    labels: dict[str, tuple[str, Optional[str]]]  # lang -> (label, short_label)
    aliases: list[tuple[str, Optional[str]]]      # (alias, lang)
    parent_code: Optional[str]
    superseded_by_code: Optional[str]

    def label(self, lang: str) -> str:
        return self.labels.get(lang, ("-", None))[0]

    def short(self, lang: str) -> Optional[str]:
        return self.labels.get(lang, ("-", None))[1]

    def links(self) -> str:
        parts = []
        if self.parent_code:
            parts.append(f"parent={self.parent_code}")
        if self.superseded_by_code:
            parts.append(f"→{self.superseded_by_code}")
        return "  ".join(parts)


# ────────────────────────────────────────────────────────────────────── writing
class OrmSeeder:
    """Seeds lookup types and values into SQLite via the ORM object graph.

    Typical use, and the only sequence that is correct::

        seeder = OrmSeeder("sqlite:///lookups.db")
        seeder.migrate()                        # the package's own Alembic chain
        report = seeder.seed(*default_seeds())

    ``migrate`` before ``seed``, always: the chain creates the four tables, and a
    seed against a database without them fails on the first SELECT rather than
    doing something useful. Both are idempotent, so the whole sequence is safe on
    every boot of a host.
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        # check_same_thread is SQLite's requirement under a threaded server, not
        # an Asas one. Set here so one engine serves both a script and a host.
        self.url = url
        self._engine: Engine = create_engine(
            url, echo=echo, connect_args={"check_same_thread": False}
        )

    @property
    def engine(self) -> Engine:
        """The engine, for a host that needs its own sessions off the same file."""
        return self._engine

    def migrate(self) -> None:
        """Apply the package-owned Alembic chain. First call that touches the file.

        Adopt-or-create: finding its tables present with no version table, it
        concludes the host's own history created them and stamps the baseline —
        which is shape-verified first, so a host that already owns a table of the
        same name gets a loud error rather than a silently skipped baseline.
        """
        migrate(self._engine)

    def seed(self, *paths: Path) -> SeedReport:
        """Seed every file given, in order, into one report."""
        report = SeedReport()
        with Session(self._engine) as session:
            for path in paths:
                if Path(path).exists():
                    self._seed_file(session, Path(path), report)
        return report

    # ---------------------------------------------------------------- internals
    def _seed_file(self, session: Session, path: Path, report: SeedReport) -> None:
        # load() validates the whole file before this touches the database, so a
        # bad edit cannot half-write a type.
        for spec in load(path):
            self._seed_type(session, spec, report)

    def _seed_type(self, session: Session, spec: SeedType, report: SeedReport) -> None:
        """One type, in four stages: the type row, the graph, the commit, the pointers."""
        type_row = self._get_or_create_type(session, spec, report)
        present = self._existing_codes(session, type_row)

        curated = SortMode(spec.default_sort or SortMode.label) == SortMode.sort_order
        written = 0
        for index, value in enumerate(spec.values):
            if value.code in present:
                report.values_skipped += 1
                continue
            # Appending to the relationship is the whole trick: the value is never
            # told its type_id, and its children never their value_id.
            type_row.values.append(self._build_value(value, index, curated))
            written += 1

        session.add(type_row)
        session.commit()  # type, values, translations, aliases — in FK order
        session.refresh(type_row)
        report.values_created += written

        linked = self._link_pointers(session, type_row, spec)
        report.pointers_linked += linked

        # The read API's ETag is keyed on version, so bump it only when something
        # actually changed — otherwise every client refetches an identical list
        # after every boot, and the 304 the package works to offer is wasted.
        if written or linked:
            type_row.version += 1
            session.add(type_row)
            session.commit()
            report.versions_bumped.append(spec.key)

    def _get_or_create_type(
        self, session: Session, spec: SeedType, report: SeedReport
    ) -> LookupType:
        """The one link the graph cannot own, because the type may already exist.

        A re-seed must return the stored row untouched — a deployment that renamed
        a type keeps its name — so this is a get-or-create, matched on ``key``
        alone, and not part of the object graph.
        """
        row = session.exec(select(LookupType).where(LookupType.key == spec.key)).first()
        if row is not None:
            report.types_existing.append(spec.key)
            return row

        row = LookupType(
            key=spec.key,
            name=spec.name,
            description=spec.description,
            is_open=spec.is_open,
            is_hierarchical=spec.is_hierarchical,
            code_system=spec.code_system,
            scope=TypeScope(spec.scope or TypeScope.platform),
            default_sort=SortMode(spec.default_sort or SortMode.label),
        )
        session.add(row)
        report.types_created.append(spec.key)
        return row

    @staticmethod
    def _existing_codes(session: Session, type_row: LookupType) -> set[str]:
        """Codes this type already holds as platform rows, so a re-seed skips them.

        A type created moments ago has ``id is None`` and holds nothing, so the
        query is skipped — which is also why this branch only ever runs on a
        second boot. ``session.exec`` on a single-column select yields the scalar,
        not a one-tuple; unpacking it raises, and only there.
        """
        if type_row.id is None:
            return set()
        return set(
            session.exec(
                select(LookupValue.code).where(
                    LookupValue.type_id == type_row.id,
                    LookupValue.org_id.is_(None),  # the seed owns platform rows
                )
            ).all()
        )

    def _build_value(self, value: SeedValue, index: int, curated: bool) -> LookupValue:
        """One value with its translations and aliases attached, and no ids set.

        Nothing here knows a ``type_id`` or a ``value_id``. Both are filled by the
        unit of work when the graph is flushed, in foreign-key order.

        ``sort_order`` falls back to the array index for a curated type — the order
        a reader sees in the file becomes the order the API returns — and to 0 for
        a label-sorted one, where the column is never read and a nonzero value
        would imply an ordering the API does not honour.
        """
        order = value.sort_order
        if order is None:
            order = index if curated else 0

        row = LookupValue(
            # org_id stays NULL: a seed owns platform rows only. For a platform
            # type that IS the row every org reads; for an org type it is the
            # template seed_org_lookups copies per org.
            org_id=None,
            code=value.code,
            status=LookupStatus(value.status or LookupStatus.active),
            is_default=value.is_default,
            sort_order=order,
            valid_from=self._as_date(value.valid_from),
            valid_to=self._as_date(value.valid_to),
            meta=dict(value.meta),
        )
        for translation in value.translations:
            row.translations.append(
                LookupTranslation(
                    lang=translation["lang"],
                    label=translation["label"],
                    short_label=translation.get("short_label"),
                )
            )
        for alias in value.aliases:
            row.aliases.append(LookupAlias(lang=alias.get("lang"), alias=alias["alias"]))
        return row

    @staticmethod
    def _link_pointers(session: Session, type_row: LookupType, spec: SeedType) -> int:
        """Resolve ``parent_code`` and ``superseded_by`` to row ids. Returns how many.

        Runs after every value of the type exists, because both name a **code** and
        the target may sit further down the file. Filled only while NULL — the same
        rule the library's ``meta`` backfill follows, and for the same reason: a
        row an admin re-parented must not be dragged back at the next boot.
        """
        wanted = {
            v.code: (v.parent_code, v.superseded_by)
            for v in spec.values
            if v.parent_code or v.superseded_by
        }
        if not wanted:
            return 0

        rows = {
            r.code: r
            for r in session.exec(
                select(LookupValue).where(
                    LookupValue.type_id == type_row.id,
                    LookupValue.org_id.is_(None),
                )
            )
        }
        linked = 0
        for code, (parent_code, superseded_code) in wanted.items():
            row = rows.get(code)
            if row is None:
                continue
            for column, target_code in (
                ("parent_id", parent_code),
                ("superseded_by_id", superseded_code),
            ):
                if not target_code or getattr(row, column) is not None:
                    continue
                target = rows.get(target_code)
                if target is None:
                    # load() already proved the code is in the file, so reaching
                    # here means the row was removed from the database by hand.
                    raise ValueError(
                        f"{spec.key}:{code} points at {target_code!r}, "
                        "which this type has no value for"
                    )
                setattr(row, column, target.id)
                session.add(row)
                linked += 1
        if linked:
            session.commit()
        return linked

    @staticmethod
    def _as_date(raw: Optional[str]) -> Optional[date]:
        """An ISO date from the file, or None. Validated at load; this only parses."""
        return date.fromisoformat(raw) if raw else None


# ────────────────────────────────────────────────────────────────────── reading
class Inspector:
    """Reads what the seeder wrote, and prints it in a shape a person can scan.

    Separate from the seeder on purpose. Writing rows and describing them are
    different jobs with different failure modes, and mixing them is how a seeder
    ends up unusable from a host that wants no console output at all. Everything
    here is read-only: it creates nothing, so it is safe to point at a database
    another process is serving.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def counts(self) -> dict[str, int]:
        with Session(self._engine) as session:
            return {
                model.__tablename__: len(session.exec(select(model)).all())
                for model in _TABLES
            }

    def types(self) -> list[LookupType]:
        with Session(self._engine) as session:
            return list(session.exec(select(LookupType).order_by(LookupType.id)))

    def values(self, type_key: str) -> list[ValueView]:
        """Every row of one type, with its children resolved.

        Sorted by ``sort_order`` then ``code`` rather than by the type's own
        ``default_sort``: this is a database view, not the API's, and an order that
        shows the stored column is the more useful one here.
        """
        with Session(self._engine) as session:
            type_row = session.exec(
                select(LookupType).where(LookupType.key == type_key)
            ).one()
            rows = session.exec(
                select(LookupValue).where(LookupValue.type_id == type_row.id)
            ).all()
            by_id = {r.id: r for r in rows}

            views: list[ValueView] = []
            for row in sorted(rows, key=lambda r: (r.sort_order, r.code)):
                labels = {
                    t.lang: (t.label, t.short_label)
                    for t in session.exec(
                        select(LookupTranslation).where(
                            LookupTranslation.value_id == row.id
                        )
                    )
                }
                aliases = [
                    (a.alias, a.lang)
                    for a in session.exec(
                        select(LookupAlias).where(LookupAlias.value_id == row.id)
                    )
                ]
                parent = by_id.get(row.parent_id)
                superseded = by_id.get(row.superseded_by_id)
                views.append(
                    ValueView(
                        row=row,
                        labels=labels,
                        aliases=aliases,
                        parent_code=parent.code if parent else None,
                        superseded_by_code=superseded.code if superseded else None,
                    )
                )
            return views

    def org_rows(self) -> dict[str, int]:
        """Rows a tenant owns, per type. Non-empty only after ``seed_org_lookups``."""
        with Session(self._engine) as session:
            out: dict[str, int] = {}
            for type_row in session.exec(select(LookupType).order_by(LookupType.key)):
                n = len(
                    session.exec(
                        select(LookupValue).where(
                            LookupValue.type_id == type_row.id,
                            LookupValue.org_id.is_not(None),
                        )
                    ).all()
                )
                if n:
                    out[type_row.key] = n
            return out

    def describe(
        self, *, verbose: bool = False, langs: tuple[str, ...] = ("en", "ar")
    ) -> None:
        self._rule("WHAT IS IN THE DATABASE")
        for type_row in self.types():
            views = self.values(type_row.key)
            print(
                f"\n  lookup_type  id={type_row.id:<3} key={type_row.key:<16}"
                f" scope={type_row.scope.value:<9}"
                f" open={str(type_row.is_open):<5}"
                f" hier={str(type_row.is_hierarchical):<5}"
                f" version={type_row.version}"
                f"   {len(views)} values"
            )
            if not verbose:
                continue
            for view in views:
                shown = "  ".join(f"{view.label(lang):<24}" for lang in langs)
                short = view.short(langs[0])
                print(
                    f"      id={view.row.id:<4} {view.row.code:<20} {shown}"
                    f" sort={view.row.sort_order:<3}"
                    f" {view.row.status.value:<10}"
                    f" {('short=' + short) if short else '':<12}"
                    f" {view.links()}"
                )
                if view.aliases:
                    print(f"            aliases {view.aliases}")

        org = self.org_rows()
        if org:
            self._rule("ROWS A TENANT OWNS")
            print("  written by seed_org_lookups, never by a seed file\n")
            for key, n in org.items():
                print(f"  {key:<18} {n:>4} rows")

        self._rule("ROW COUNTS")
        for table, n in self.counts().items():
            print(f"  {table:<20} {n:>5} rows")

    @staticmethod
    def _rule(title: str) -> None:
        print(f"\n{_RULE}\n  {title}\n{_RULE}")


# ────────────────────────────────────────────────────────────────── command line
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m asas_lookups.orm_seeding",
        description="Seed the asas-lookups tables into a SQLite file via the ORM.",
    )
    parser.add_argument(
        "--db", required=True, type=Path, help="path to the SQLite file"
    )
    parser.add_argument(
        "--seed",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help="a host's own seed file, in addition to the library's. Repeatable.",
    )
    parser.add_argument(
        "--fresh", action="store_true", help="delete the file before seeding"
    )
    parser.add_argument(
        "--show", action="store_true", help="describe what is there; seed nothing"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="counts and types only, no value rows"
    )
    parser.add_argument(
        "--echo", action="store_true", help="log every statement SQLAlchemy emits"
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    # The seeded labels are Arabic as often as English, and a Windows console
    # defaults to a codepage that cannot encode them — which turns a working run
    # into a UnicodeEncodeError traceback.
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    args = _parser().parse_args(argv)
    db: Path = args.db

    if args.fresh and db.exists():
        db.unlink()
        print(f"  removed {db}")

    if args.show and not db.exists():
        print(f"  {db} does not exist — run without --show to create it")
        return 1

    seeder = OrmSeeder(f"sqlite:///{db}", echo=args.echo)

    if not args.show:
        files = (*default_seeds(), *args.seed)
        print(f"\n{_RULE}\n  SEEDING → {db}\n{_RULE}")
        for path in files:
            print(f"  {'reading' if path.exists() else 'MISSING'}  {path.name}")

        seeder.migrate()
        report = seeder.seed(*files)

        print(f"\n  {report.summary()}")
        if report.types_created:
            print(f"  created: {', '.join(report.types_created)}")
        if report.versions_bumped:
            print(f"  version bumped: {', '.join(report.versions_bumped)}")
        if report.wrote_nothing:
            print("  nothing changed — this run was idempotent")

    Inspector(seeder.engine).describe(verbose=not args.quiet)
    if db.exists():
        print(f"\n  file: {db}  ({db.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
