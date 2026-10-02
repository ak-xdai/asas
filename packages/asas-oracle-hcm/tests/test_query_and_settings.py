"""The ``q`` grammar, the row readers, and settings validation."""

import pytest

from asas_oracle_hcm import (
    OracleConfigError,
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


def test_literal_strips_single_quotes_because_oracle_has_no_escape():
    assert literal("O'Brien") == "OBrien"
    assert eq("LastName", "O'Brien") == "LastName='OBrien'"


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
