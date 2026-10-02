"""The HTTP layer: one async client per Oracle Fusion instance.

Worth knowing about the upstream:

- ``onlyData=true`` strips the HATEOAS ``links`` block, which triples a
  payload. Every read sets it; a caller that needs the links (an attachment's
  enclosure key lives only there) passes ``onlyData="false"``.
- A field list NARROWS the response. Ask for no projection and a record comes
  back with the NAMES Fusion denormalises beside its ids (a ``PhaseName``
  beside a ``PhaseId``); ask for ``fields=...PhaseId`` and the names are
  dropped, leaving bare ids that no lookup resource resolves.
- A PATCH wants ``application/vnd.oracle.adf.resourceitem+json`` and refuses
  plain JSON; a POST wants plain JSON and refuses the ADF type.
- A duplicate key on a create comes back as a 400 with prose, not a 409, so the
  status alone cannot tell it apart (see :class:`OracleAlreadyExistsError`).

**No retries here.** Whether and when to retry is the caller's policy: an
outbox row counts its own attempts, and a second retry budget inside the client
would be invisible to it. The one exception is a connection that could not be
OPENED (``OracleSettings.connect_retries``), which sent nothing and so cannot
be a duplicate. A PATCH by id is idempotent (the same body twice
leaves the record where once did) and so is safe for a caller to retry; a
state TRANSITION endpoint such as ``POST .../action/move`` is not, because a
repeat advances the record again.

**A circuit breaker guards the reads** (:class:`asas_oracle_hcm.Breaker`): after
consecutive faults they fail at once with :class:`OracleUnavailableError`
instead of each waiting out the timeout. Writes are counted, never refused.
:attr:`OracleFusionClient.health` carries the breaker and per-resource call
statistics for a status page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, AsyncIterator, NamedTuple

import httpx

from .cache import Cache, CachePolicy, MemoryCache
from .errors import (
    OracleAlreadyExistsError,
    OracleNotConfiguredError,
    OracleNotFoundError,
    OracleUnavailableError,
    OracleUpstreamError,
)
from .settings import OracleSettings
from .upstream import Breaker, UpstreamHealth

logger = logging.getLogger(__name__)

#: Oracle's own page cap on most resources (see :mod:`asas_oracle_hcm.paging`
#: for the cap and the offset ceiling).
MAX_PAGE_SIZE = 200

_ADF_ITEM = "application/vnd.oracle.adf.resourceitem+json"

#: The words Oracle's duplicate-key refusal is known to use. Matched only to
#: choose an error CLASS, never shown, and kept loose: a refusal we do not
#: recognise falls through to the generic upstream error, the fail-closed side.
_DUPLICATE_MARKERS = ("already exists", "duplicate", "unique")


def _reads_as_duplicate(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DUPLICATE_MARKERS)


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _resource(path: str) -> str:
    return path.strip("/").split("/", 1)[0].split("?", 1)[0]


class CollectionPage(NamedTuple):
    """One page of a collection. Unpacks as ``items, has_more, total``.

    ``total`` appears only when the caller asks with ``totalResults=true``, and
    some resources answer ``-1`` even then: that is Oracle declining to count,
    NOT an empty collection, so it reads as ``None``. Oracle's own ``count`` is
    the size of this page and is never the total."""

    items: list[dict[str, Any]]
    has_more: bool
    total: int | None


class OracleFusionClient:
    """Basic-auth JSON client for one Oracle Fusion instance.

    Holds one ``httpx.AsyncClient`` so the TLS handshake is paid once. A host
    that needs a private CA, a proxy or a shared pool passes its own as
    ``http=``; the library uses it as-is and does not close it. Close the
    client at shutdown with ``await client.aclose()`` or ``async with``.
    """

    def __init__(
        self,
        settings: OracleSettings,
        *,
        http: httpx.AsyncClient | None = None,
        cache: Cache | None = None,
        cache_policy: CachePolicy | None = None,
        cache_namespace: str = "asas:oracle",
        breaker: Breaker | None = None,
    ) -> None:
        self._settings = settings
        self._borrowed = http
        self._owned: httpx.AsyncClient | None = None
        self._cache: Cache = cache if cache is not None else MemoryCache()
        self._policy = cache_policy or CachePolicy()
        # Keys are scoped to the instance, so a test pod and production can
        # share one store without sharing answers.
        scope = hashlib.sha1(settings.base_url.encode()).hexdigest()[:10]
        self._ns = f"{cache_namespace}:{scope}"
        self._health = UpstreamHealth(breaker if breaker is not None else Breaker())

    # -- lifecycle -------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self._settings.configured

    @property
    def health(self) -> UpstreamHealth:
        """The breaker and the call statistics (``health.snapshot()``)."""
        return self._health

    @property
    def base_url(self) -> str:
        """Safe to show: credentials travel in the Authorization header."""
        return self._settings.base_url

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        s = self._settings
        if s.gateway_api_key:
            headers[s.gateway_api_key_header] = s.gateway_api_key
        return headers

    def _http(self) -> httpx.AsyncClient:
        if not self._settings.configured:
            raise OracleNotConfiguredError("Oracle HCM is not configured for this deployment.")
        if self._borrowed is not None:
            return self._borrowed
        if self._owned is None:
            s = self._settings
            transport = httpx.AsyncHTTPTransport(
                retries=s.connect_retries,
                limits=httpx.Limits(
                    max_connections=s.max_connections,
                    max_keepalive_connections=s.max_connections,
                    keepalive_expiry=30.0,
                ),
            )
            self._owned = httpx.AsyncClient(timeout=s.timeout_seconds, transport=transport)
        return self._owned

    def _url(self, path: str) -> str:
        return f"{self._settings.base_url}/{path.lstrip('/')}"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        http = self._http()
        s = self._settings
        health = self._health
        breaker = health.breaker
        # Reads are guarded; writes are counted but never refused, because the
        # caller's outbox owns their retry and cadence.
        if method == "GET" and not breaker.allow():
            health.refused += 1
            raise OracleUnavailableError(
                "Oracle is unavailable; not asking again until the cooldown ends.",
                retry_after_seconds=breaker.retry_after_seconds(),
                method=method,
                path=path,
            )
        resource = _resource(path) or "root"
        started = time.perf_counter()
        try:
            response = await http.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
                headers={**self._headers(), **(headers or {})},
                auth=s.basic_auth,
                timeout=s.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            health.record(method, resource, _ms(started), fault=True)
            logger.warning("oracle %s %s: transport failure: %s", method, path, exc)
            raise OracleUpstreamError(
                "Oracle is unreachable.", method=method, path=path
            ) from exc
        except BaseException:
            # Cancelled mid-call: no verdict, but a probe must not stay claimed.
            breaker.release_probe()
            raise
        code = response.status_code
        health.record(method, resource, _ms(started), fault=code >= 500 or code == 429)
        if response.status_code >= 400:
            text = response.text[:500]
            logger.warning("oracle %s %s answered %s", method, path, response.status_code)
            logger.debug("oracle %s %s body: %s", method, path, text)
            kind: type[OracleUpstreamError] = OracleUpstreamError
            if response.status_code == 404:
                kind = OracleNotFoundError
            elif (
                method == "POST"
                and response.status_code in (400, 409)
                and _reads_as_duplicate(text)
            ):
                kind = OracleAlreadyExistsError
            raise kind(
                f"Oracle answered {response.status_code}.",
                status=response.status_code,
                method=method,
                path=path,
            )
        return response

    @staticmethod
    def _json(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise OracleUpstreamError(
                "Oracle returned a malformed body.", method=method, path=path
            ) from exc
        if not isinstance(body, dict):
            raise OracleUpstreamError(
                "Oracle returned an unexpected body.", method=method, path=path
            )
        return body

    async def aclose(self) -> None:
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None

    async def __aenter__(self) -> OracleFusionClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- the cache -------------------------------------------------------------

    def _version_key(self, resource: str) -> str:
        return f"{self._ns}:ver:{resource}"

    async def _cache_key(self, resource: str, path: str, query: dict[str, Any]) -> str:
        version = await self._cache.get_int(self._version_key(resource))
        digest = hashlib.sha1(
            json.dumps([path, sorted((k, str(v)) for k, v in query.items())]).encode()
        ).hexdigest()
        return f"{self._ns}:{resource}:v{version}:{digest}"

    async def _invalidate(self, path: str) -> None:
        stale = self._policy.stale_on_write.get(_resource(path), ())
        longest = max(self._policy.ttls.values(), default=0)
        for resource in stale:
            await self._cache.incr(self._version_key(resource), max(longest, 86_400))

    # -- verbs -----------------------------------------------------------------

    async def get(
        self, path: str, params: dict[str, Any] | None = None, *, use_cache: bool = True
    ) -> dict[str, Any]:
        """GET one resource or collection and return its decoded body.

        ``onlyData=true`` is sent unless the caller passes ``onlyData`` itself.
        ``None`` values in ``params`` are dropped. Read-through cached per the
        :class:`CachePolicy`; a cache miss or a failing cache falls through to
        Oracle unchanged.

        ``use_cache=False`` goes to Oracle whatever the cache holds and stores
        nothing. Use it when a layer ABOVE the client (a persistent lookup
        store, a mirror) has decided its own answer is out of date: a cached
        copy of the same read is no newer, and answering from it would re-save
        the old answer as fresh."""
        query: dict[str, Any] = {"onlyData": "true"}
        query.update({k: v for k, v in (params or {}).items() if v is not None})
        resource = _resource(path)
        ttl = self._policy.ttls.get(resource, 0)
        cache_key = ""
        if use_cache and ttl > 0 and self._settings.configured:
            cache_key = await self._cache_key(resource, path, query)
            cached = await self._cache.get(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("body"), dict):
                return cached["body"]
        response = await self._send("GET", path, params=query)
        body = self._json(response, "GET", path)
        if cache_key:
            if body.get("items") == []:
                ttl = min(ttl, self._policy.empty_answer_ttl_seconds)
            await self._cache.set(cache_key, {"path": path, "body": body}, ttl)
        return body

    async def get_collection(
        self, path: str, params: dict[str, Any] | None = None, *, use_cache: bool = True
    ) -> CollectionPage:
        """GET a collection as a :class:`CollectionPage` (``use_cache`` as in
        :meth:`get`)."""
        body = await self.get(path, params, use_cache=use_cache)
        items = body.get("items")
        rows = [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []
        raw_total = body.get("totalResults")
        total = (
            int(raw_total)
            if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0
            else None
        )
        return CollectionPage(rows, bool(body.get("hasMore")), total)

    async def iter_collection(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        page_size: int = MAX_PAGE_SIZE,
        max_pages: int | None = None,
        use_cache: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Every row of a collection, paging by offset until Oracle says there
        is no more (or a page comes back empty).

        Pass a stable ``orderBy`` (an id) in ``params`` when the collection can
        change during the walk, or rows can shift between pages. ``max_pages``
        bounds the walk for a collection too large to read whole."""
        offset = 0
        pages = 0
        base = dict(params or {})
        while True:
            page = await self.get_collection(
                path, {**base, "limit": page_size, "offset": offset}, use_cache=use_cache
            )
            for row in page.items:
                yield row
            pages += 1
            if not page.has_more or not page.items:
                return
            if max_pages is not None and pages >= max_pages:
                return
            offset += page_size

    async def get_bytes(self, path: str) -> tuple[bytes, str]:
        """GET a binary enclosure: ``(content, content_type)``. No ``onlyData``
        and ``Accept: */*``, because an enclosure answers ``406`` to a JSON
        Accept header. Read whole, not streamed."""
        response = await self._send("GET", path, headers={"Accept": "*/*"})
        content_type = response.headers.get("content-type", "application/octet-stream")
        return response.content, content_type.split(";")[0].strip()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST (create) and return the record Oracle answers. A refusal that
        names a taken key raises :class:`OracleAlreadyExistsError`."""
        await self._invalidate(path)
        response = await self._send(
            "POST", path, json_body=body, headers={"Content-Type": "application/json"}
        )
        answered = self._json(response, "POST", path)
        await self._invalidate(path)
        return answered

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH a record by id and return the updated record, so a caller can
        read back what actually landed."""
        await self._invalidate(path)
        response = await self._send(
            "PATCH", path, json_body=body, headers={"Content-Type": _ADF_ITEM}
        )
        answered = self._json(response, "PATCH", path)
        await self._invalidate(path)
        return answered
