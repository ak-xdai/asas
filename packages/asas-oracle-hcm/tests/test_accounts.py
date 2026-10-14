"""The user account behind an address, and the role it holds.

Every assertion here is a trap that answers 200 when it is wrong, which is
why they are worth pinning: a permission read that quietly says "no" refuses
the right people and looks like policy.
"""

import asyncio

import pytest

from asas_oracle_hcm import (
    OracleGatewayError,
    OracleUpstreamError,
    find_account,
    holds_any_role,
    usernames_for,
)

from conftest import collection


def run(coro):
    return asyncio.run(coro)


ADDRESS = "Jane.Doe@dge.gov.ae"


# ---- the spellings ----------------------------------------------------------


def test_with_no_suffix_an_address_is_asked_as_given_and_lowercased():
    assert usernames_for(ADDRESS) == [ADDRESS, "jane.doe@dge.gov.ae"]


def test_an_already_lowercase_address_is_asked_ONCE():
    assert usernames_for("jane@x.ae") == ["jane@x.ae"]


def test_a_scrubbing_pod_asks_the_SUFFIXED_spellings_first():
    """On such a pod nearly every username is suffixed, so the plain
    spellings are the unlikely ones and must not lead."""
    assert usernames_for(ADDRESS, "123") == [
        f"{ADDRESS}123",
        "jane.doe@dge.gov.ae123",
        ADDRESS,
        "jane.doe@dge.gov.ae",
    ]


# ---- finding the account ----------------------------------------------------


def test_the_username_route_answers_without_touching_the_worker(client, oracle):
    oracle.route("/userAccounts", collection({"GUID": "G1", "SuspendedFlag": False}))
    account = run(find_account(client, ADDRESS))
    assert account["GUID"] == "G1"
    assert all("publicWorkers" not in c.path for c in oracle.calls)


def test_every_spelling_is_asked_AT_ONCE(client, oracle):
    """Sequentially, a miss on the likely spelling costs a whole round trip:
    measured at 6.4s for three, against a 5s budget in the UI."""
    oracle.route("/userAccounts", collection())
    oracle.route("/publicWorkers", collection())
    run(find_account(client, ADDRESS, "123"))
    account_calls = [c for c in oracle.calls if "userAccounts" in c.path]
    assert len(account_calls) == 4  # the four spellings, together


def test_the_work_email_route_is_the_fallback(client, oracle):
    oracle.route("/userAccounts", collection())
    oracle.route("/publicWorkers", collection({"PersonId": 300}))
    seen: list[str] = []

    original = client.get_collection

    async def spy(path, params=None, **kw):
        seen.append(str((params or {}).get("q")))
        if "userAccounts" in path and "PersonId" in str((params or {}).get("q")):
            return ([{"GUID": "G9", "SuspendedFlag": False}], False, None)
        return await original(path, params, **kw)

    client.get_collection = spy  # type: ignore[method-assign]
    account = run(find_account(client, ADDRESS))
    assert account["GUID"] == "G9"
    assert any("WorkEmail" in q for q in seen)


def test_the_scrubbed_placeholder_is_never_looked_up(client, oracle):
    """It is every worker's address on a scrubbed pod, so it would find
    whichever sorts first: a different human."""
    oracle.route("/userAccounts", collection())
    assert run(find_account(client, "sendmail-test-discard@oracle.com")) is None
    assert all("publicWorkers" not in c.path for c in oracle.calls)


# ---- the role ---------------------------------------------------------------


def _holds(oracle, client, codes, roles_answer):
    oracle.route("/userAccounts", collection({"GUID": "G1", "SuspendedFlag": False}))
    oracle.route("/userAccounts/G1/child/userAccountRoles", roles_answer)
    return run(holds_any_role(client, ADDRESS, codes))


def test_a_held_role_is_True(client, oracle):
    assert _holds(oracle, client, ["HM"], collection({"RoleCode": "HM"})) is True


def test_an_unheld_role_is_False(client, oracle):
    assert _holds(oracle, client, ["HM"], collection()) is False


def test_the_code_is_CHECKED_in_the_answer_not_trusted_from_the_filter(client, oracle):
    """Under the default framework version an unsupported filter is not
    refused: it answers 200 with rows that do not match. Trusting the filter
    would read any row as the role being held."""
    assert _holds(oracle, client, ["HM"], collection({"RoleCode": "SOMETHING_ELSE"})) is False


def test_it_stops_at_the_first_code_held(client, oracle):
    oracle.route("/userAccounts", collection({"GUID": "G1", "SuspendedFlag": False}))
    oracle.route("/userAccounts/G1/child/userAccountRoles", collection({"RoleCode": "A"}))
    assert run(holds_any_role(client, ADDRESS, ["A", "B", "C"])) is True
    role_calls = [c for c in oracle.calls if "userAccountRoles" in c.path]
    assert len(role_calls) == 1


def test_one_equality_PER_CODE_and_never_an_IN_clause(client, oracle):
    oracle.route("/userAccounts", collection({"GUID": "G1", "SuspendedFlag": False}))
    oracle.route("/userAccounts/G1/child/userAccountRoles", collection())
    run(holds_any_role(client, ADDRESS, ["A", "B"]))
    asked = [c.params["q"] for c in oracle.calls if "userAccountRoles" in c.path]
    assert asked == ["RoleCode='A'", "RoleCode='B'"]
    assert not any(" IN " in q or " or " in q for q in asked)


def test_a_SUSPENDED_account_holds_nothing(client, oracle):
    oracle.route("/userAccounts", collection({"GUID": "G1", "SuspendedFlag": True}))
    assert run(holds_any_role(client, ADDRESS, ["HM"])) is False


def test_no_codes_asks_oracle_nothing(client, oracle):
    assert run(holds_any_role(client, ADDRESS, [])) is False
    assert oracle.calls == []


def test_an_UNREACHABLE_resource_RAISES_rather_than_answering_no(client, oracle):
    """A collection query never 404s for a person Oracle does not know, so a
    404 means the operation is not carried at all (a gateway, typically).
    Answering False there would silently refuse everybody."""
    oracle.route("/userAccounts", (404, {"detail": "no matching resource"}))
    with pytest.raises(OracleUpstreamError):
        run(holds_any_role(client, ADDRESS, ["HM"]))


def test_a_gateway_refusal_also_reaches_the_caller(client, oracle):
    oracle.route("/userAccounts", (500, {"detail": "Transport protocol not supported"}))
    with pytest.raises(OracleGatewayError):
        run(holds_any_role(client, ADDRESS, ["HM"]))
