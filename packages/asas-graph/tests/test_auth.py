"""The MSAL provider, against a fake MSAL application (no network)."""

import asyncio

import pytest

from asas_graph import ClientCredentialTokenProvider, GraphAuthError, StaticTokenProvider


class FakeMsalApp:
    def __init__(self, silent=None, fresh=None):
        self._silent, self._fresh = silent, fresh
        self.silent_calls, self.fresh_calls = [], []

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls.append(list(scopes))
        return self._silent

    def acquire_token_for_client(self, scopes):
        self.fresh_calls.append(list(scopes))
        return self._fresh


def _provider(settings, app):
    factories = []

    def factory(s):
        factories.append(s)
        return app

    return ClientCredentialTokenProvider(settings, app_factory=factory), factories


def test_a_cached_token_is_served_without_a_network_call(settings):
    app = FakeMsalApp(silent={"access_token": "cached"})
    provider, _ = _provider(settings, app)
    assert asyncio.run(provider.access_token()) == "cached"
    assert app.fresh_calls == []
    assert app.silent_calls == [["https://graph.microsoft.com/.default"]]


def test_a_cache_miss_acquires_for_client(settings):
    app = FakeMsalApp(silent=None, fresh={"access_token": "fresh"})
    provider, _ = _provider(settings, app)
    assert asyncio.run(provider.access_token()) == "fresh"
    assert app.fresh_calls == [["https://graph.microsoft.com/.default"]]


def test_the_msal_application_is_built_once_and_reused(settings):
    """MSAL's token cache lives on the application object: rebuilding it per
    call would throw the cache away and hit the token endpoint every time."""
    app = FakeMsalApp(silent={"access_token": "t"})
    provider, factories = _provider(settings, app)
    asyncio.run(provider.access_token())
    asyncio.run(provider.access_token())
    assert factories == [settings]


def test_an_msal_error_becomes_a_typed_auth_error(settings):
    app = FakeMsalApp(
        silent=None,
        fresh={"error": "invalid_client", "error_description": "AADSTS7000215: bad secret",
               "correlation_id": "corr-1"},
    )
    provider, _ = _provider(settings, app)
    with pytest.raises(GraphAuthError) as exc:
        asyncio.run(provider.access_token())
    assert exc.value.reason == "AADSTS7000215: bad secret"
    assert exc.value.detail == {"error": "invalid_client", "correlation_id": "corr-1"}


def test_an_empty_msal_result_is_still_an_auth_error(settings):
    provider, _ = _provider(settings, FakeMsalApp(silent=None, fresh=None))
    with pytest.raises(GraphAuthError, match="no access_token"):
        asyncio.run(provider.access_token())


def test_static_provider_returns_its_token_and_rejects_empty():
    assert asyncio.run(StaticTokenProvider("abc").access_token()) == "abc"
    with pytest.raises(GraphAuthError):
        StaticTokenProvider("")
