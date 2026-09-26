"""Shared fakes. Nothing here touches the network: httpx is replaced by a
``MockTransport`` that routes each request to a scripted answer and records it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from asas_oracle_hcm import OracleFusionClient, OracleSettings

BASE = "https://pod.example.com/hcmRestApi/resources/11.13.18.05"


@dataclass
class Recorded:
    method: str
    path: str
    params: dict[str, str]
    headers: httpx.Headers
    json: Any


@dataclass
class FakeOracle:
    """A scripted Fusion instance.

    ``route(path, answer)`` registers an answer for a resource path (relative to
    the REST root, without the query). An answer is a status plus a JSON body,
    or a callable taking the recorded request and returning an
    ``httpx.Response``. Unrouted paths answer an empty collection."""

    routes: dict[str, Any] = field(default_factory=dict)
    calls: list[Recorded] = field(default_factory=list)

    def route(self, path: str, answer: Any) -> None:
        self.routes[path] = answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        path = parts.path[len(urlsplit(BASE).path):]
        params = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        payload = json.loads(request.content) if request.content else None
        rec = Recorded(request.method, path, params, request.headers, payload)
        self.calls.append(rec)
        answer = self.routes.get(path, (200, {"items": [], "hasMore": False}))
        if callable(answer):
            return answer(rec)
        status, body = answer
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    def calls_to(self, path: str) -> list[Recorded]:
        return [c for c in self.calls if c.path == path]


@pytest.fixture
def oracle() -> FakeOracle:
    return FakeOracle()


@pytest.fixture
def settings() -> OracleSettings:
    return OracleSettings(base_url=BASE, username="integration", password="s3cret")


@pytest.fixture
def make_client(oracle: FakeOracle) -> Callable[..., OracleFusionClient]:
    def build(settings: OracleSettings | None = None, **kwargs: Any) -> OracleFusionClient:
        s = settings or OracleSettings(base_url=BASE, username="integration", password="s3cret")
        http = httpx.AsyncClient(transport=httpx.MockTransport(oracle.handler))
        return OracleFusionClient(s, http=http, **kwargs)

    return build


@pytest.fixture
def client(make_client: Callable[..., OracleFusionClient]) -> OracleFusionClient:
    return make_client()


def collection(*rows: dict[str, Any], has_more: bool = False, total: Any = None) -> tuple[int, dict]:
    body: dict[str, Any] = {"items": list(rows), "hasMore": has_more, "count": len(rows)}
    if total is not None:
        body["totalResults"] = total
    return 200, body
