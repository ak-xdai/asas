"""Shared fakes. Nothing here touches the network: MSAL is replaced by a fake
application object and httpx by a ``MockTransport`` that records requests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from asas_graph import GraphClient, GraphSettings, StaticTokenProvider


@pytest.fixture
def settings() -> GraphSettings:
    return GraphSettings(
        tenant_id="tenant-1", client_id="client-1", client_secret="s3cret"
    )


@dataclass
class Recorded:
    method: str
    url: str
    headers: httpx.Headers
    json: Any


@dataclass
class FakeGraph:
    """A scripted Graph: ``respond`` decides the reply, ``calls`` remembers what was sent."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    calls: list[Recorded] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        self.calls.append(Recorded(request.method, str(request.url), request.headers, payload))
        if self.body is None:
            return httpx.Response(self.status, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    @property
    def only(self) -> Recorded:
        assert len(self.calls) == 1, self.calls
        return self.calls[0]


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def client(settings: GraphSettings, graph: FakeGraph) -> GraphClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(graph.handler))
    return GraphClient(settings, token_provider=StaticTokenProvider("tok-123"), http=http)
