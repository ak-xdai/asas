"""The route a deployment takes, and the four behaviours that come with it.

All four were carried here from a host that had them in its own client, and
each exists because the symptom without it is a 401, a 415 or a silently
parked row rather than anything that names itself.
"""

import asyncio

import pytest

from asas_oracle_hcm import (
    OracleFusionClient,
    OracleGatewayError,
    OracleLockedError,
    OracleSettings,
    OracleUpstreamError,
    is_gateway,
    misroute,
)

from conftest import BASE, collection

DIRECT = "https://pod.example.com/hcmRestApi/resources/11.13.18.05"
GATEWAY = "https://stage-api.example.ae/gateway/HR_ENDPOINTS/1.0"


def run(coro):
    return asyncio.run(coro)


# ---- telling the two addresses apart ----------------------------------------


def test_an_address_is_judged_by_its_SHAPE_not_its_host():
    # Host names differ per client, so the path is the only stable tell.
    assert is_gateway(GATEWAY) is True
    assert is_gateway(DIRECT) is False
    assert is_gateway("") is False


# ---- the contradictions -----------------------------------------------------


def test_a_gateway_deployment_pointed_straight_at_fusion_is_refused():
    code, message = misroute("gateway", DIRECT)
    assert code == "base_url_is_direct"
    assert "directly" in message


def test_a_direct_deployment_pointed_at_a_gateway_is_refused():
    code, _ = misroute("direct", GATEWAY)
    assert code == "base_url_is_gateway"


def test_a_gateway_needs_its_key():
    assert misroute("gateway", GATEWAY, has_api_key=False)[0] == "gateway_key_missing"


def test_a_gateway_needs_the_ACCOUNT_TOO_because_the_key_alone_is_a_401():
    assert misroute("gateway", GATEWAY, has_account=False)[0] == "gateway_account_missing"


@pytest.mark.parametrize("route", ["direct", "gateway"])
def test_NO_ADDRESS_IS_NOT_A_CONTRADICTION(route):
    """Switched off and misconfigured are different states, and reporting the
    first as the second sends somebody hunting a fault that is not there."""
    assert misroute(route, "", has_api_key=False, has_account=False) == ("", "")


def test_a_route_that_agrees_says_nothing():
    assert misroute("gateway", GATEWAY) == ("", "")
    assert misroute("direct", DIRECT) == ("", "")


def test_no_reason_names_a_secret_or_an_environment_variable():
    """The package is domain-agnostic: what a host calls its vault entries is
    the host's business, and a message naming one is wrong everywhere else."""
    for args in (
        ("gateway", DIRECT, {}),
        ("gateway", GATEWAY, {"has_api_key": False}),
        ("gateway", GATEWAY, {"has_account": False}),
        ("direct", GATEWAY, {}),
    ):
        route, url, kwargs = args
        _code, message = misroute(route, url, **kwargs)
        lowered = message.lower()
        assert "adrbe" not in lowered and "_" not in message


# ---- the empty Content-Type on a gateway read -------------------------------


def _client(make_client, route, **kw):
    return make_client(
        OracleSettings(
            base_url=BASE, username="integration", password="s3cret", route=route, **kw
        )
    )


def test_a_gateway_READ_sends_an_EMPTY_content_type(make_client, oracle):
    """Measured: the gateway answers 415 to a GET with no Content-Type and to
    one with application/json alike, and 200 to an empty one."""
    c = _client(make_client, "gateway")
    oracle.route("/grades", collection({"GradeId": 1}))
    run(c.get("/grades"))
    assert oracle.calls[0].headers.get("content-type") == ""


def test_the_direct_route_sends_no_such_header(make_client, oracle):
    c = _client(make_client, "direct")
    oracle.route("/grades", collection({"GradeId": 1}))
    run(c.get("/grades"))
    assert (oracle.calls[0].headers.get("content-type") or "") == ""


def test_a_gateway_WRITE_states_its_media_type_and_never_inherits_the_blank(
    make_client, oracle
):
    """A body sent with a blank Content-Type is refused, so the read's own
    default must not reach a write."""
    c = _client(make_client, "gateway")
    oracle.route("/recruitingJobRequisitions", (201, {"RequisitionNumber": "1"}))
    run(c.post("/recruitingJobRequisitions", {"Title": "x"}))
    assert oracle.calls[0].headers["content-type"].startswith("application/")


# ---- the two refusals that are not Oracle's own -----------------------------


def test_a_row_lock_is_its_own_TRANSIENT_error(client, oracle):
    oracle.route(
        "/recruitingJobRequisitions/1",
        (400, {"detail": "Failed to lock the record in table IRC_REQUISITIONS_VL"}),
    )
    with pytest.raises(OracleLockedError):
        run(client.patch("/recruitingJobRequisitions/1", {"NumberOfOpenings": 2}))


def test_a_gateway_refusal_is_told_apart_from_oracles_own(client, oracle):
    oracle.route("/userAccounts", (500, {"detail": "Transport protocol not supported"}))
    with pytest.raises(OracleGatewayError):
        run(client.get("/userAccounts"))


def test_an_unrecognised_refusal_stays_the_GENERIC_error(client, oracle):
    """The markers are loose on purpose, so anything they do not match falls
    through to the fail-closed side rather than being labelled a guess."""
    oracle.route("/grades", (400, {"detail": "something nobody has seen before"}))
    with pytest.raises(OracleUpstreamError) as caught:
        run(client.get("/grades"))
    assert type(caught.value) is OracleUpstreamError


# ---- the cache scope --------------------------------------------------------


def test_the_scope_is_the_address_by_default_and_two_pods_never_share_one():
    a = OracleFusionClient(OracleSettings(base_url=DIRECT, username="u", password="p"))
    b = OracleFusionClient(OracleSettings(base_url=GATEWAY, username="u", password="p"))
    assert a.cache_scope and a.cache_scope != b.cache_scope


def test_a_host_may_NAME_the_scope_so_its_own_keys_sit_in_the_same_segment():
    named = OracleFusionClient(
        OracleSettings(base_url=DIRECT, username="u", password="p", cache_scope="dge-dev4")
    )
    assert named.cache_scope == "dge-dev4"
