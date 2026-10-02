"""The ``q`` grammar, the row readers, and settings validation."""

import pytest

from asas_oracle_hcm import (
    OracleConfigError,
    OracleQueryError,
    OracleSettings,
    and_,
    child_items,
    eq,
    flag,
    integer,
    like,
    literal,
    text,
)


def test_a_quote_is_refused_unless_stripping_is_asked_for():
    """Oracle has no escape form, so a quote is refused rather than silently
    dropped (which used to search O'Brien as OBrien and match nothing)."""
    with pytest.raises(OracleQueryError):
        literal("O'Brien")
    with pytest.raises(OracleQueryError):
        eq("LastName", "O'Brien")
    assert literal("O'Brien", strip_quotes=True) == "OBrien"
    assert eq("LastName", "O'Brien", strip_quotes=True) == "LastName='OBrien'"


@pytest.mark.parametrize("value", ["1;BusinessUnitId=999", "a;b"])
def test_a_semicolon_cannot_start_another_clause(value):
    """`;` is Oracle's AND: in a value it would widen the query."""
    with pytest.raises(OracleQueryError):
        eq("Name", value)
    with pytest.raises(OracleQueryError):
        eq("Name", value, strip_quotes=True)
    with pytest.raises(OracleQueryError):
        like("Name", value)


@pytest.mark.parametrize("value", ["1;BusinessUnitId=999", "1 OR 1=1", "1'", "", "PersonId>0"])
def test_an_unquoted_value_must_be_a_plain_id(value):
    with pytest.raises(OracleQueryError):
        eq("PersonId", value, quote=False)


@pytest.mark.parametrize("field", ["Name;x", "Name='a'", "", "1Name", "Name Name"])
def test_a_field_name_must_be_an_attribute_name(field):
    with pytest.raises(OracleQueryError):
        eq(field, "a")


def test_query_errors_are_value_errors_and_oracle_errors():
    from asas_oracle_hcm import OracleError

    with pytest.raises(ValueError):
        literal("a;b")
    with pytest.raises(OracleError):
        literal("a;b")


def test_eq_quotes_by_default_and_can_leave_a_numeric_id_bare():
    assert eq("PersonNumber", 100) == "PersonNumber='100'"
    assert eq("PersonId", "300000008607150", quote=False) == "PersonId=300000008607150"


def test_like_and_and_join_with_semicolons_and_skip_empties():
    assert like("Title", "archi") == "Title LIKE '%archi%'"
    assert and_("A='1'", None, "", "B='2'") == "A='1';B='2'"
    assert and_(None, "") is None


def test_row_readers_normalise_oracles_mixed_encodings():
    row = {"a": None, "b": 5, "y": "Y", "t": True, "n": "N", "num": "42", "bad": "x", "bool": True}
    assert text(row, "a") == "" and text(row, "b") == "5" and text(row, "missing") == ""
    assert flag(row, "y") and flag(row, "t") and not flag(row, "n") and not flag(row, "missing")
    assert integer(row, "num") == 42
    assert integer(row, "bad") is None and integer(row, "bool") is None and integer(row, "a") is None


def test_child_items_reads_a_bare_list_and_a_collection_object():
    assert child_items({"c": [{"x": 1}, "junk"]}, "c") == [{"x": 1}]
    assert child_items({"c": {"items": [{"x": 2}]}}, "c") == [{"x": 2}]
    assert child_items({}, "c") == [] and child_items({"c": "nope"}, "c") == []


def test_settings_accept_off_and_refuse_half_wired():
    assert OracleSettings().configured is False
    s = OracleSettings(base_url="https://pod.example.com/rest/", username="u", password="p")
    assert s.configured and s.base_url == "https://pod.example.com/rest"
    with pytest.raises(OracleConfigError):
        OracleSettings(base_url="https://pod.example.com", username="u")
    with pytest.raises(OracleConfigError):
        OracleSettings(base_url="pod.example.com", username="u", password="p")
    with pytest.raises(OracleConfigError):
        OracleSettings(timeout_seconds=0)
    with pytest.raises(OracleConfigError):
        OracleSettings(gateway_api_key="k", gateway_api_key_header=" ")


def test_from_env_reads_the_prefix(monkeypatch):
    monkeypatch.setenv("ACME_BASE_URL", "https://pod.example.com")
    monkeypatch.setenv("ACME_USERNAME", "u")
    monkeypatch.setenv("ACME_PASSWORD", "p")
    monkeypatch.setenv("ACME_GATEWAY_API_KEY", "gw")
    s = OracleSettings.from_env("ACME_")
    assert s.configured and s.gateway_api_key == "gw" and s.gateway_api_key_header == "x-api-key"
