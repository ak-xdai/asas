"""The seed data is JSON now, so the JSON is what has to be guarded.

Moving the shipped vocabulary out of Python literals removed a real safety net: a
typo in a Python list is a syntax error at import, while a typo in JSON is a key
nobody reads. These tests are that net.

Four classes. The first checks the content actually shipped, so a bad edit fails
here rather than on a host's first boot. The second checks seeding still honours
the behaviours the hand-written per-type blocks carried. The third drives every
column a file may set, using a host fixture, because the shipped data is flat,
active and label-only and exercises almost none of them. The fourth checks the
loader refuses the shapes a careless edit produces.
"""

import json
from datetime import date

import pytest
from sqlmodel import Session, select

import asas_lookups
from asas_lookups.data import SCHEMA, SEED_FILE, load, types
from asas_lookups.models import (
    LookupAlias,
    LookupStatus,
    LookupTranslation,
    LookupType,
    LookupValue,
    SortMode,
)

# What the library ships. Locked deliberately: TEAMY-803 removed seventeen types
# because a second host was inheriting the first one's product vocabulary, and
# nothing but a test stops that creeping back one well-meaning type at a time.
SHIPPED = {
    "country",
    "currency",
    "gender",
    "marital_status",
    "nationality",
    "salutation",
}


def _langs(value) -> dict[str, dict]:
    return {t["lang"]: t for t in value.translations}


def _value(session: Session, type_key: str, code: str) -> LookupValue:
    type_ = session.exec(select(LookupType).where(LookupType.key == type_key)).one()
    return session.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_.id, LookupValue.code == code
        )
    ).one()


def _doc(type_specs: list[dict]) -> dict:
    return {"$schema": SCHEMA, "types": type_specs}


def _write(tmp_path, doc) -> object:
    path = tmp_path / "s.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


class TestShippedData:
    def test_the_file_is_found_and_parses(self):
        assert list(types()), "seed.json declares no types"

    def test_it_is_one_file(self):
        # The whole vocabulary in one reviewable place. A second file would
        # reintroduce the question this answered: which one holds the list you
        # are looking for?
        assert SEED_FILE.name == "seed.json"
        siblings = sorted(p.name for p in SEED_FILE.parent.glob("*.json"))
        assert siblings == ["seed.json"]

    def test_ships_exactly_the_agreed_types(self):
        assert {t.key for t in types()} == SHIPPED

    def test_type_keys_are_unique(self):
        keys = [t.key for t in types()]
        assert len(keys) == len(set(keys))

    def test_every_value_carries_english(self):
        for spec in types():
            for value in spec.values:
                assert _langs(value).get("en", {}).get(
                    "label"
                ), f"{spec.key}:{value.code} has no en label"

    def test_every_value_carries_arabic(self):
        # The package's whole claim to being bilingual. A value that shipped with
        # English only degrades silently: the read API falls back to another
        # language rather than erroring, so an Arabic reader sees English and
        # nobody is told.
        for spec in types():
            for value in spec.values:
                assert _langs(value).get("ar", {}).get(
                    "label"
                ), f"{spec.key}:{value.code} has no ar label"

    def test_country_and_nationality_hold_the_same_code_set(self):
        # This shape gives each type its own values, so the 249 ISO codes and
        # their aliases are now maintained TWICE. Nothing but this test stops the
        # two drifting apart, which is the price of the nesting.
        by_key = {t.key: t for t in types()}
        country, nationality = by_key["country"], by_key["nationality"]
        assert len(country.values) == len(nationality.values) == 249
        assert {v.code for v in country.values} == {v.code for v in nationality.values}

    def test_country_and_nationality_hold_the_same_aliases(self):
        by_key = {t.key: t for t in types()}
        aliased = lambda spec: {
            v.code: sorted(a["alias"] for a in v.aliases) for v in spec.values
        }
        assert aliased(by_key["country"]) == aliased(by_key["nationality"])

    def test_arabic_is_written_literally_not_escaped(self):
        # Written with ensure_ascii=False. A diff full of \\u0623 sequences cannot
        # be reviewed by the Arabic speaker who has to check it, which defeats the
        # point of moving this content out of source code.
        raw = SEED_FILE.read_text(encoding="utf-8")
        json.loads(raw)
        assert "\\u06" not in raw, f"{SEED_FILE.name} contains escaped Arabic"


class TestSeedingFromData:
    def test_curated_types_take_the_order_the_file_states(self, session: Session):
        asas_lookups.seed(session)
        salutation = session.exec(
            select(LookupType).where(LookupType.key == "salutation")
        ).one()
        assert salutation.default_sort == SortMode.sort_order

        in_file = [
            (v.code, v.sort_order)
            for t in types()
            if t.key == "salutation"
            for v in t.values
        ]
        rows = session.exec(
            select(LookupValue)
            .where(LookupValue.type_id == salutation.id)
            .order_by(LookupValue.sort_order)
        ).all()
        assert [(r.code, r.sort_order) for r in rows] == in_file

    def test_label_sorted_types_leave_sort_order_alone(self, session: Session):
        # Nothing reads the column for these, and a nonzero value would imply an
        # ordering the API does not honour.
        asas_lookups.seed(session)
        country = session.exec(
            select(LookupType).where(LookupType.key == "country")
        ).one()
        orders = {
            r.sort_order
            for r in session.exec(
                select(LookupValue).where(LookupValue.type_id == country.id)
            )
        }
        assert orders == {0}

    def test_meta_reaches_new_rows_and_only_the_ones_declaring_it(
        self, session: Session
    ):
        asas_lookups.seed(session)
        assert _value(session, "salutation", "dr").meta.get("show_in_name") is True
        assert "show_in_name" not in (_value(session, "salutation", "mr").meta or {})

    def test_meta_backfills_an_absent_key(self, session: Session):
        # The case the flag was added for: rows that shipped before the key existed.
        asas_lookups.seed(session)
        dr = _value(session, "salutation", "dr")
        dr.meta = {}
        session.add(dr)
        session.commit()

        asas_lookups.seed(session)
        session.refresh(dr)
        assert dr.meta.get("show_in_name") is True

    def test_meta_never_overwrites_an_explicit_admin_edit(self, session: Session):
        # An admin who turned the flag off must not find it back on after the next
        # boot. This is the whole reason it is a backfill and not a write.
        asas_lookups.seed(session)
        dr = _value(session, "salutation", "dr")
        dr.meta = {"show_in_name": False}
        session.add(dr)
        session.commit()

        asas_lookups.seed(session)
        session.refresh(dr)
        assert dr.meta["show_in_name"] is False

    def test_seeding_twice_changes_nothing(self, session: Session):
        asas_lookups.seed(session)
        snapshot = {
            (v.type_id, v.code, v.sort_order, json.dumps(v.meta, sort_keys=True))
            for v in session.exec(select(LookupValue))
        }
        versions = {t.key: t.version for t in session.exec(select(LookupType))}

        asas_lookups.seed(session)
        assert {
            (v.type_id, v.code, v.sort_order, json.dumps(v.meta, sort_keys=True))
            for v in session.exec(select(LookupValue))
        } == snapshot
        # An unchanged seed must not bump the version either: the ETag is keyed on
        # it, so every client would refetch an identical list after every boot.
        assert {t.key: t.version for t in session.exec(select(LookupType))} == versions


class TestEveryColumnAFileMaySet:
    """One host fixture drives every settable column.

    The shipped types are flat, active, label-only vocabulary and touch almost
    none of this, so without a fixture exercising the rest these columns would be
    reachable only from the admin API and nobody would notice them breaking.
    """

    FIXTURE = _doc(
        [
            {
                "key": "region",
                "name": "Region",
                "description": "Emirates and their sub-regions.",
                "is_open": False,
                "is_hierarchical": True,
                "code_system": "internal",
                "scope": "platform",
                "default_sort": "sort_order",
                "values": [
                    # Deliberately BEFORE its parent: a file is not required to be
                    # topologically sorted, and the second pass is what makes that
                    # true.
                    {
                        "code": "al_ain",
                        "sort_order": 2,
                        "parent_code": "abu_dhabi",
                        "translations": [
                            {"lang": "en", "label": "Al Ain"},
                            {"lang": "ar", "label": "العين"},
                        ],
                    },
                    {
                        "code": "abu_dhabi",
                        "status": "active",
                        "is_default": True,
                        "sort_order": 1,
                        "valid_from": "2020-01-01",
                        "meta": {"iso": "AE-AZ"},
                        "translations": [
                            {"lang": "en", "label": "Abu Dhabi", "short_label": "AD"},
                            {"lang": "ar", "label": "أبوظبي", "short_label": None},
                        ],
                        "aliases": [
                            {"lang": None, "alias": "AUH"},
                            {"lang": "en", "alias": "Abu Zaby"},
                        ],
                    },
                    # Retired, and superseded by a code declared further DOWN.
                    {
                        "code": "western_region",
                        "status": "deprecated",
                        "sort_order": 4,
                        "valid_to": "2023-12-31",
                        "superseded_by": "al_dhafra",
                        "parent_code": "abu_dhabi",
                        "translations": [{"lang": "en", "label": "Western Region"}],
                    },
                    {
                        "code": "al_dhafra",
                        "sort_order": 3,
                        "parent_code": "abu_dhabi",
                        "translations": [{"lang": "en", "label": "Al Dhafra"}],
                    },
                ],
            }
        ]
    )

    @pytest.fixture()
    def seeded(self, session: Session, tmp_path):
        path = _write(tmp_path, self.FIXTURE)
        asas_lookups.seed_file(session, path)
        return path

    def _rows(self, session):
        type_ = session.exec(select(LookupType).where(LookupType.key == "region")).one()
        rows = session.exec(
            select(LookupValue).where(LookupValue.type_id == type_.id)
        ).all()
        return type_, {r.code: r for r in rows}, {r.id: r for r in rows}

    def test_type_columns_reach_the_row(self, session: Session, seeded):
        type_, _, _ = self._rows(session)
        assert type_.is_hierarchical is True
        assert type_.description == "Emirates and their sub-regions."
        assert type_.code_system == "internal"
        assert type_.default_sort == SortMode.sort_order

    def test_parent_code_resolves_forward(self, session: Session, seeded):
        _, by_code, by_id = self._rows(session)
        assert by_id[by_code["al_ain"].parent_id].code == "abu_dhabi"
        assert by_code["abu_dhabi"].parent_id is None

    def test_superseded_by_resolves_forward(self, session: Session, seeded):
        _, by_code, by_id = self._rows(session)
        assert by_id[by_code["western_region"].superseded_by_id].code == "al_dhafra"

    def test_status_and_validity_dates(self, session: Session, seeded):
        _, by_code, _ = self._rows(session)
        assert by_code["western_region"].status is LookupStatus.deprecated
        assert by_code["western_region"].valid_to == date(2023, 12, 31)
        assert by_code["abu_dhabi"].status is LookupStatus.active
        assert by_code["abu_dhabi"].valid_from == date(2020, 1, 1)

    def test_is_default(self, session: Session, seeded):
        _, by_code, _ = self._rows(session)
        assert by_code["abu_dhabi"].is_default is True
        assert by_code["al_ain"].is_default is False

    def test_explicit_sort_order_is_honoured(self, session: Session, seeded):
        # The file states 1..4 in an order that is not the array order, so an
        # index-derived value would disagree with it.
        _, by_code, _ = self._rows(session)
        assert [
            code
            for code, _ in sorted(by_code.items(), key=lambda kv: kv[1].sort_order)
        ] == ["abu_dhabi", "al_ain", "al_dhafra", "western_region"]

    def test_short_labels_land_on_the_right_language(self, session: Session, seeded):
        _, by_code, _ = self._rows(session)
        labels = {
            tr.lang: (tr.label, tr.short_label)
            for tr in session.exec(
                select(LookupTranslation).where(
                    LookupTranslation.value_id == by_code["abu_dhabi"].id
                )
            )
        }
        assert labels["en"] == ("Abu Dhabi", "AD")
        # Arabic has a label but an explicit null short one, so it stays NULL
        # rather than borrowing the English.
        assert labels["ar"][1] is None

    def test_aliases_keep_their_language(self, session: Session, seeded):
        _, by_code, _ = self._rows(session)
        aliases = {
            a.alias: a.lang
            for a in session.exec(
                select(LookupAlias).where(
                    LookupAlias.value_id == by_code["abu_dhabi"].id
                )
            )
        }
        assert aliases == {"AUH": None, "Abu Zaby": "en"}

    def test_org_id_is_never_set_by_a_seed(self, session: Session, seeded):
        # A seed owns platform rows only. For a platform type this is the row
        # every org reads; for an org type it is the template seed_org_lookups
        # copies. Either way a file cannot claim an org.
        _, by_code, _ = self._rows(session)
        assert all(r.org_id is None for r in by_code.values())

    def test_reseeding_never_moves_a_pointer(self, session: Session, seeded):
        # Same rule as the meta backfill: pointers are filled while NULL, never
        # rewritten, so a row an admin re-parented survives the next boot.
        _, by_code, _ = self._rows(session)
        moved = by_code["al_ain"]
        moved.parent_id = None
        session.add(moved)
        session.commit()

        asas_lookups.seed_file(session, seeded)
        session.refresh(moved)
        assert moved.parent_id is not None  # refilled because it was NULL

        _, by_code, _ = self._rows(session)
        moved.parent_id = by_code["al_dhafra"].id  # an admin's own choice
        session.add(moved)
        session.commit()

        asas_lookups.seed_file(session, seeded)
        session.refresh(moved)
        _, _, by_id = self._rows(session)
        assert by_id[moved.parent_id].code == "al_dhafra"  # left alone

    def test_seeding_the_fixture_twice_changes_nothing(self, session: Session, seeded):
        type_, by_code, _ = self._rows(session)
        shape = lambda rows: {
            (r.code, r.status, r.is_default, r.sort_order, r.parent_id,
             r.superseded_by_id, r.valid_from, r.valid_to)
            for r in rows
        }
        before, version = shape(by_code.values()), type_.version

        asas_lookups.seed_file(session, seeded)
        type_, by_code, _ = self._rows(session)
        assert shape(by_code.values()) == before
        assert type_.version == version


class TestLoaderRejectsBadData:
    """Every rule, refused at load, before anything touches a database."""

    def _bad(self, tmp_path, doc, match):
        with pytest.raises(ValueError, match=match):
            load(_write(tmp_path, doc))

    T = {"key": "x", "name": "X"}
    V = [{"code": "a", "translations": [{"lang": "en", "label": "A"}]}]

    # ---- the contract itself
    def test_a_missing_or_wrong_schema(self, tmp_path):
        self._bad(tmp_path, {"types": [{**self.T, "values": self.V}]}, "this loader reads")
        self._bad(
            tmp_path,
            {"$schema": "lookup_seed_v2", "types": [{**self.T, "values": self.V}]},
            "this loader reads",
        )

    def test_no_types(self, tmp_path):
        self._bad(tmp_path, _doc([]), "no types declared")

    def test_a_duplicate_type_key(self, tmp_path):
        # The second entry would silently reuse the first's type row and append
        # its values to that vocabulary.
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": self.V}, {**self.T, "values": self.V}]),
            "declared twice",
        )

    # ---- required fields
    def test_a_type_with_no_key(self, tmp_path):
        self._bad(tmp_path, _doc([{"name": "X", "values": []}]), "no 'key'")

    def test_a_type_with_no_name(self, tmp_path):
        # Caught at load rather than seed. Without this it reached ensure_type and
        # surfaced as a bare KeyError from inside the package.
        self._bad(tmp_path, _doc([{"key": "x", "values": []}]), "no 'name'")

    def test_a_value_with_no_code(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{"translations": [{"lang": "en", "label": "A"}]}]}]),
            "value with no 'code'",
        )

    def test_a_value_with_no_translations(self, tmp_path):
        self._bad(
            tmp_path, _doc([{**self.T, "values": [{"code": "a"}]}]), "no translations"
        )

    def test_a_translation_with_no_lang_or_label(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{"code": "a", "translations": [{"label": "A"}]}]}]),
            "has no 'lang'",
        )
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{"code": "a", "translations": [{"lang": "en"}]}]}]),
            "has no 'label'",
        )

    def test_two_translations_for_one_language(self, tmp_path):
        # uq_translation_value_lang would refuse the second row mid-seed, leaving
        # the type half-written.
        self._bad(
            tmp_path,
            _doc([
                {**self.T, "values": [{
                    "code": "a",
                    "translations": [
                        {"lang": "en", "label": "A"},
                        {"lang": "en", "label": "Again"},
                    ],
                }]}
            ]),
            "two 'en' translations",
        )

    def test_an_alias_with_no_alias(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{
                "code": "a",
                "translations": [{"lang": "en", "label": "A"}],
                "aliases": [{"lang": "en"}],
            }]}]),
            "has no 'alias'",
        )

    # ---- values and enums
    def test_a_duplicate_code(self, tmp_path):
        self._bad(
            tmp_path, _doc([{**self.T, "values": self.V + self.V}]), "duplicate code"
        )

    def test_an_unknown_status(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{**self.V[0], "status": "retired"}]}]),
            "expected one of",
        )

    def test_a_date_that_is_not_iso(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{**self.V[0], "valid_to": "31-12-2023"}]}]),
            "expected an ISO date",
        )

    # ---- pointers
    def test_a_parent_code_the_type_does_not_have(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "is_hierarchical": True,
                   "values": [{**self.V[0], "parent_code": "ghost"}]}]),
            "not a code of type",
        )

    def test_a_parent_code_on_a_flat_type(self, tmp_path):
        # parent_id on a type nobody declared hierarchical populates a column no
        # read path walks — a hierarchy that silently is not one.
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [
                {**self.V[0], "parent_code": "b"},
                {"code": "b", "translations": [{"lang": "en", "label": "B"}]},
            ]}]),
            "not declared is_hierarchical",
        )

    def test_a_value_pointing_at_itself(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "is_hierarchical": True,
                   "values": [{**self.V[0], "parent_code": "a"}]}]),
            "to itself",
        )

    def test_a_superseded_by_the_type_does_not_have(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{**self.V[0], "superseded_by": "ghost"}]}]),
            "not a code of type",
        )

    # ---- scope
    def test_an_open_list_on_a_platform_type(self, tmp_path):
        # An open list means org users add values, and only an org-owned type can
        # host that. ensure_type raises the same way, but a file is worth failing
        # before it reaches a database.
        self._bad(
            tmp_path,
            _doc([{**self.T, "is_open": True, "scope": "platform", "values": self.V}]),
            "requires scope 'org'",
        )

    # ---- keys that would otherwise vanish
    def test_a_forbidden_column_is_refused_with_its_reason(self, tmp_path):
        # org_id in particular: silently ignoring it would let an author believe a
        # file can hand a value to one tenant, which it never can.
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{**self.V[0], "org_id": 4}]}]),
            "seed owns platform rows only",
        )
        self._bad(
            tmp_path,
            _doc([{**self.T, "version": 9, "values": self.V}]),
            "bump_version_if owns it",
        )

    def test_the_id_form_of_a_pointer_is_refused(self, tmp_path):
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{**self.V[0], "parent_id": 2}]}]),
            "as 'parent_code'",
        )

    def test_the_old_labels_shape_is_refused(self, tmp_path):
        # The previous contract used a labels mapping. A file still written that
        # way would otherwise fail on "no translations" and say nothing useful.
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{"code": "a", "labels": {"en": "A"}}]}]),
            "under 'translations'",
        )

    def test_a_misspelt_key_does_not_vanish(self, tmp_path):
        # The failure mode the move to JSON introduced: a Python typo was a
        # NameError, a JSON typo is a key nobody reads.
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{"code": "a", "translatons": []}]}]),
            "unknown key 'translatons'",
        )
        self._bad(
            tmp_path,
            _doc([{**self.T, "default_order": "label", "values": self.V}]),
            "unknown key 'default_order'",
        )
        self._bad(
            tmp_path,
            _doc([{**self.T, "values": [{
                "code": "a",
                "translations": [{"lang": "en", "label": "A", "shortlabel": "A"}],
            }]}]),
            "unknown key 'shortlabel'",
        )
