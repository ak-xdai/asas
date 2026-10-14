import pytest

from asas_graph import GraphConfigError, GraphSettings


def test_authority_joins_host_and_tenant():
    s = GraphSettings(tenant_id="t", client_id="c", client_secret="x")
    assert s.authority == "https://login.microsoftonline.com/t"
    assert s.base_url == "https://graph.microsoft.com/v1.0"
    assert s.scopes == ("https://graph.microsoft.com/.default",)


def test_trailing_slashes_are_normalised():
    s = GraphSettings(
        tenant_id="t", client_id="c", client_secret="x",
        authority_host="https://login.microsoftonline.us/",
        base_url="https://graph.microsoft.us/v1.0/",
    )
    assert s.authority == "https://login.microsoftonline.us/t"
    assert s.base_url == "https://graph.microsoft.us/v1.0"


@pytest.mark.parametrize("missing", ["tenant_id", "client_id", "client_secret"])
def test_missing_credentials_fail_at_construction(missing):
    kwargs = {"tenant_id": "t", "client_id": "c", "client_secret": "x", missing: ""}
    with pytest.raises(GraphConfigError, match=missing):
        GraphSettings(**kwargs)


def test_scopes_and_timeout_are_validated():
    with pytest.raises(GraphConfigError, match="scope"):
        GraphSettings(tenant_id="t", client_id="c", client_secret="x", scopes=())
    with pytest.raises(GraphConfigError, match="timeout"):
        GraphSettings(tenant_id="t", client_id="c", client_secret="x", timeout_seconds=0)


def test_from_env_reads_required_and_optional_variables():
    env = {
        "GRAPH_TENANT_ID": "t",
        "GRAPH_CLIENT_ID": "c",
        "GRAPH_CLIENT_SECRET": "x",
        "GRAPH_BASE_URL": "https://graph.microsoft.us/v1.0",
        "GRAPH_SCOPES": "https://graph.microsoft.us/.default",
        "GRAPH_TIMEOUT_SECONDS": "12.5",
    }
    s = GraphSettings.from_env(env=env)
    assert (s.tenant_id, s.client_id, s.client_secret) == ("t", "c", "x")
    assert s.base_url == "https://graph.microsoft.us/v1.0"
    assert s.scopes == ("https://graph.microsoft.us/.default",)
    assert s.timeout_seconds == 12.5
    assert s.authority_host == "https://login.microsoftonline.com"  # default kept


def test_from_env_names_every_missing_variable():
    with pytest.raises(GraphConfigError) as exc:
        GraphSettings.from_env(env={"GRAPH_TENANT_ID": "t"})
    assert "GRAPH_CLIENT_ID" in str(exc.value) and "GRAPH_CLIENT_SECRET" in str(exc.value)
    assert "GRAPH_TENANT_ID" not in str(exc.value)


def test_from_env_honours_a_custom_prefix():
    env = {"MS_TENANT_ID": "t", "MS_CLIENT_ID": "c", "MS_CLIENT_SECRET": "x"}
    assert GraphSettings.from_env(prefix="MS_", env=env).tenant_id == "t"


def test_from_env_rejects_a_non_numeric_timeout():
    env = {"GRAPH_TENANT_ID": "t", "GRAPH_CLIENT_ID": "c", "GRAPH_CLIENT_SECRET": "x",
           "GRAPH_TIMEOUT_SECONDS": "soon"}
    with pytest.raises(GraphConfigError, match="TIMEOUT_SECONDS"):
        GraphSettings.from_env(env=env)
