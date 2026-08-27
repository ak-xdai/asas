"""The per-org overlay through the configure_org_resolver hook: org rows shadow
platform seeds, never leak across orgs; unconfigured hosts stay single-tenant."""

import pytest
from sqlmodel import Session

from asas_lookups import ensure_type, service
from asas_lookups.models import LookupTranslation, LookupValue, SortMode, TypeScope

# The library seeds no open type of its own (TEAMY-803 moved the host-owned
# vocabularies out), and these tests need one to exercise get_or_create. Making
# it a fixture is the honest shape anyway: an open type is something a *host*
# registers, so the test plays the host.
OPEN_TYPE = "test_open_type"


@pytest.fixture()
def open_type(seeded):
    with Session(seeded) as s:
        ensure_type(
            s,
            key=OPEN_TYPE,
            name="Test open type",
            is_open=True,
            scope=TypeScope.org,
            code_system="internal",
            default_sort=SortMode.label,
        )
        s.commit()
    return OPEN_TYPE


def _labels(client, type_key="gender"):
    return {v["label"] for v in client.get(f"/lookups/{type_key}").json()["items"]}


def test_unconfigured_resolver_is_single_tenant(client, seeded, open_type):
    """No resolver configured → global rows only, no error, and values created
    without an org context land as global (NULL org)."""
    with Session(seeded) as s:
        code = service.get_or_create_value(s, open_type, "Plain Hosting")
        s.commit()
        type_ = service.get_type(s, open_type)
        row = service._value_by_code(s, type_, code)
        assert row is not None and row.org_id is None
    assert "Plain Hosting" in _labels(client, open_type)


def test_org_value_shadows_and_scopes(client, seeded, open_type, org):
    # Org 1 creates its own value in an open type via get_or_create.
    org.org_id = 1
    with Session(seeded) as s:
        service.get_or_create_value(s, open_type, "Underwater Basketweaving")
        s.commit()
    assert "Underwater Basketweaving" in _labels(client, open_type)
    etag_org1 = client.get(f"/lookups/{open_type}").headers["ETag"]

    # Org 2 doesn't see org 1's value; its ETag differs (no cross-org 304s).
    org.org_id = 2
    assert "Underwater Basketweaving" not in _labels(client, open_type)
    assert client.get(f"/lookups/{open_type}").headers["ETag"] != etag_org1

    # Global seeds reach every org.
    assert len(_labels(client, "gender")) > 0


def test_org_label_override_wins(client, seeded, org):
    """An org row with the same code as a platform seed shadows it in listings.

    Note the admin API rejects creating a code the caller can already see (409), so
    same-code pairs arise out-of-band — e.g. a later platform seed ships a code an
    org had already minted. Simulate that by inserting the org row directly."""
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        row = LookupValue(type_id=type_.id, org_id=7, code="male")
        s.add(row)
        s.flush()
        s.add(LookupTranslation(value_id=row.id, lang="en", label="Male (Org 7)"))
        type_.version += 1  # ETag cache-bust, as any service write would do
        s.add(type_)
        s.commit()

    org.org_id = 7
    labels = _labels(client, "gender")
    assert "Male (Org 7)" in labels
    # the shadowed global label is collapsed away, not duplicated
    codes = [v["code"] for v in client.get("/lookups/gender").json()["items"]]
    assert codes.count("male") == 1
    # other orgs still see the platform label
    org.org_id = None
    assert "Male (Org 7)" not in _labels(client, "gender")
