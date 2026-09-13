"""Prompts as data, served from a registry with a local copy underneath.

A prompt is a :class:`Prompt`: a name, a version, a text body or a list of chat
messages, and the config the prompt's author attached (model, temperature).
Variables use the Langfuse ``{{name}}`` spelling in every store, so a prompt
snapshotted from Langfuse and a prompt written by hand in a file are the same
thing to the runner.

A :class:`PromptStore` is anything with ``get(name, label=, version=)`` and
``names()``. Three ship here and one more in :mod:`asas_llm.langfuse`:

* :class:`InMemoryPromptStore`: prompts declared in code. Tests, and hosts that
  do not want a registry at all.
* :class:`LocalPromptStore`: one file per prompt in a directory the host ships
  with its image: ``<name>.json`` in the snapshot format, or ``<name>.md`` for
  a plain text prompt.
* :class:`LayeredPromptStore`: the design that removes the hard runtime
  dependency the roadmap names. Every read goes **registry, then last-good
  copy, then local file**, and only when all three have nothing does it raise.
  A replica that starts while the registry is unreachable serves the shipped
  copy; a replica that loses the registry mid-flight keeps serving what it last
  fetched; and both say which happened in ``Prompt.source`` and in the log.

The registry stays the place prompts are *edited*. The local copy is refreshed
by :func:`asas_llm.snapshot.snapshot` (a build step or a CI job), so "the
fallback drifted from production" is a diff in the repo, not a surprise.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import quote, unquote

from .errors import PromptFormatError, PromptUnavailable

log = logging.getLogger(__name__)

PromptType = Literal["text", "chat"]
ChatMessage = tuple[str, str]  # (role, content)

_VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}")

SNAPSHOT_FORMAT = 1


@dataclass(frozen=True)
class Prompt:
    """One prompt, as served. Immutable; ``replace`` it to change a field.

    ``content`` is a ``str`` for a text prompt and a tuple of ``(role, content)``
    pairs for a chat prompt. ``native`` is the registry's own client object when
    the prompt came from one (Langfuse's ``PromptClient``), kept so a tracing
    integration can link generations to the prompt version; it takes no part in
    equality and is dropped from snapshots.
    """

    name: str
    type: PromptType
    content: str | tuple[ChatMessage, ...]
    version: int | None = None
    labels: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    native: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.type == "chat":
            if isinstance(self.content, str):
                raise PromptFormatError(f"chat prompt {self.name!r} needs a list of messages", prompt=self.name)
            object.__setattr__(self, "content", tuple((str(r), str(c)) for r, c in self.content))
        elif not isinstance(self.content, str):
            raise PromptFormatError(f"text prompt {self.name!r} needs a string body", prompt=self.name)
        object.__setattr__(self, "labels", tuple(self.labels))
        object.__setattr__(self, "config", dict(self.config or {}))

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        """The prompt as chat messages: a text prompt becomes one system message."""
        if self.type == "chat":
            return self.content  # type: ignore[return-value]
        return (("system", self.content),)  # type: ignore[arg-type]

    def variables(self) -> tuple[str, ...]:
        """Every ``{{name}}`` in the prompt, in first-appearance order."""
        seen: dict[str, None] = {}
        for _, body in self.messages:
            for match in _VARIABLE.finditer(body):
                seen.setdefault(match.group(1), None)
        return tuple(seen)

    def compile(self, **values: Any) -> str | list[dict[str, str]]:
        """Fill ``{{name}}`` placeholders. Unknown names are left in place, which
        is what lets a second stage (a templating library) finish the job."""

        def fill(body: str) -> str:
            return _VARIABLE.sub(
                lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), body
            )

        if self.type == "text":
            return fill(self.content)  # type: ignore[arg-type]
        return [{"role": role, "content": fill(body)} for role, body in self.messages]

    def to_dict(self) -> dict[str, Any]:
        """The snapshot format: what :class:`LocalPromptStore` reads back."""
        return {
            "format": SNAPSHOT_FORMAT,
            "name": self.name,
            "type": self.type,
            "version": self.version,
            "labels": list(self.labels),
            "config": dict(self.config),
            "prompt": (
                self.content
                if self.type == "text"
                else [{"role": r, "content": c} for r, c in self.messages]
            ),
            "source": self.source,
            "snapshotted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "local") -> "Prompt":
        try:
            ptype = data["type"]
            body = data["prompt"]
            if ptype == "chat":
                content: Any = tuple((m["role"], m["content"]) for m in body)
            else:
                content = body
            return cls(
                name=data["name"],
                type=ptype,
                content=content,
                version=data.get("version"),
                labels=tuple(data.get("labels") or ()),
                config=data.get("config") or {},
                source=source,
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise PromptFormatError(f"malformed prompt record: {exc!r}", prompt=data.get("name") if isinstance(data, Mapping) else None) from exc


@runtime_checkable
class PromptStore(Protocol):
    """What the runner needs from a prompt source."""

    def get(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        """Return the prompt or raise :class:`PromptUnavailable`."""

    def names(self) -> list[str]:
        """Every prompt name this store can serve (for snapshots and warm-up)."""


class InMemoryPromptStore:
    """Prompts declared in code. ``label``/``version`` are accepted and ignored:
    there is exactly one copy of each."""

    source = "memory"

    def __init__(self, prompts: Iterable[Prompt] = ()) -> None:
        self._prompts: dict[str, Prompt] = {}
        for p in prompts:
            self.add(p)

    def add(self, prompt: Prompt) -> None:
        self._prompts[prompt.name] = replace(prompt, source=self.source)

    def text(self, name: str, body: str, **config: Any) -> Prompt:
        """Shorthand: register a text prompt and return it."""
        prompt = Prompt(name=name, type="text", content=body, config=config, source=self.source)
        self._prompts[name] = prompt
        return prompt

    def chat(self, name: str, messages: Iterable[ChatMessage], **config: Any) -> Prompt:
        prompt = Prompt(name=name, type="chat", content=tuple(messages), config=config, source=self.source)
        self._prompts[name] = prompt
        return prompt

    def get(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        try:
            return self._prompts[name]
        except KeyError:
            raise PromptUnavailable(f"no prompt {name!r} in the in-memory store", prompt=name) from None

    def names(self) -> list[str]:
        return sorted(self._prompts)


class LocalPromptStore:
    """A directory of prompt files, shipped with the host.

    ``<name>.json`` is the snapshot format (:meth:`Prompt.to_dict`), which is
    what :func:`asas_llm.snapshot.snapshot` writes. ``<name>.md`` (or ``.txt``)
    is a text prompt written by hand. Names are percent-encoded into file names
    so a registry name with ``/`` or spaces round-trips.

    ``label`` and ``version`` are accepted and ignored: a local copy is whatever
    was snapshotted, and the returned ``Prompt.version`` says which. Refusing to
    serve because the label differs would defeat the purpose of a fallback.
    """

    source = "local"
    _SUFFIXES = (".json", ".md", ".txt")

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(self, name: str, suffix: str = ".json") -> Path:
        return self.directory / f"{quote(name, safe='-_.')}{suffix}"

    def get(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        for suffix in self._SUFFIXES:
            path = self.path_for(name, suffix)
            if path.is_file():
                return self._read(path, name)
        raise PromptUnavailable(
            f"no local copy of prompt {name!r} under {self.directory}", prompt=name
        )

    def names(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        found = {unquote(p.stem) for p in self.directory.iterdir() if p.suffix in self._SUFFIXES and p.is_file()}
        return sorted(found)

    def _read(self, path: Path, name: str) -> Prompt:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptFormatError(f"cannot read {path}: {exc}", prompt=name) from exc
        if path.suffix == ".json":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PromptFormatError(f"{path} is not valid JSON: {exc}", prompt=name) from exc
            if not isinstance(data, dict):
                raise PromptFormatError(f"{path} must hold a JSON object", prompt=name)
            data.setdefault("name", name)
            prompt = Prompt.from_dict(data, source=self.source)
            if prompt.name != name:
                raise PromptFormatError(
                    f"{path} is named {prompt.name!r} inside but {name!r} on disk", prompt=name
                )
            return prompt
        return Prompt(name=name, type="text", content=raw.strip("\n"), source=self.source)


@dataclass
class _Cached:
    prompt: Prompt
    fetched_at: float


class LayeredPromptStore:
    """Registry first, last-good copy second, local file third.

    ``primary`` is the registry (normally :class:`asas_llm.langfuse.LangfusePromptStore`).
    ``fallback`` is the shipped copy (normally :class:`LocalPromptStore`); ``None``
    means "cache only", which still survives a registry outage after the first
    successful fetch but not a cold start.

    ``cache_ttl`` bounds how long a fetched prompt is served without asking the
    registry again, so an edit in the registry reaches replicas within that
    window. A **failed** refresh never evicts: the stale copy is served, marked
    ``source="cache"``, and the failure is logged once per ``log_every`` seconds
    per prompt rather than on every call.

    ``stale_ok=False`` turns the second layer off (a registry failure goes
    straight to the local file), for hosts that would rather serve the shipped
    copy than a possibly-outdated fetch.
    """

    def __init__(
        self,
        primary: PromptStore,
        fallback: PromptStore | None = None,
        *,
        cache_ttl: float = 60.0,
        stale_ok: bool = True,
        log_every: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.cache_ttl = cache_ttl
        self.stale_ok = stale_ok
        self.log_every = log_every
        self._clock = clock
        self._cache: dict[tuple[str, str | None, int | None], _Cached] = {}
        self._last_logged: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        key = (name, label, version)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None and now - cached.fetched_at < self.cache_ttl:
            return cached.prompt

        try:
            prompt = self.primary.get(name, label=label, version=version)
        except Exception as exc:  # noqa: BLE001 - any registry failure falls through
            served = self._degraded(key, cached, exc)
            # Re-stamp the window on a FAILED refresh too: without this, once
            # the TTL expires during an outage every single request re-pays
            # the full registry timeout before serving the stale copy — the
            # outage the layered store exists to absorb becomes fleet-wide
            # multi-second latency. One probe per TTL window, not per call.
            with self._lock:
                self._cache[key] = _Cached(prompt=served, fetched_at=now)
            return served

        with self._lock:
            self._cache[key] = _Cached(prompt=prompt, fetched_at=now)
        return prompt

    def _degraded(self, key: tuple[str, str | None, int | None], cached: _Cached | None, exc: Exception) -> Prompt:
        name = key[0]
        tried = [f"registry ({type(exc).__name__}: {exc})"]
        if cached is not None and self.stale_ok:
            self._log(name, f"serving last-good copy (version {cached.prompt.version})", exc)
            return replace(cached.prompt, source="cache")
        tried.append("last-good copy (none)" if cached is None else "last-good copy (disabled)")
        if self.fallback is not None:
            try:
                prompt = self.fallback.get(name, label=key[1], version=key[2])
            except Exception as fexc:  # noqa: BLE001 - a corrupt/unreadable local
                # file (PromptFormatError, OSError) must degrade like a missing
                # one: boot is the wrong moment to crash, and the documented
                # contract is one PromptUnavailable naming every layer tried.
                tried.append(f"local ({type(fexc).__name__}: {fexc})")
            else:
                self._log(name, f"serving local copy (version {prompt.version})", exc)
                return prompt
        else:
            tried.append("local (no fallback store configured)")
        raise PromptUnavailable(
            f"prompt {name!r} could not be served; tried " + "; ".join(tried), prompt=name
        ) from exc

    def _log(self, name: str, what: str, exc: Exception) -> None:
        now = self._clock()
        last = self._last_logged.get(name)
        if last is None or now - last >= self.log_every:
            self._last_logged[name] = now
            log.warning("prompt %r: registry unavailable (%s: %s); %s", name, type(exc).__name__, exc, what)

    def names(self) -> list[str]:
        try:
            return self.primary.names()
        except Exception as exc:  # noqa: BLE001
            log.warning("prompt registry unavailable while listing names (%s); using local names", exc)
            return self.fallback.names() if self.fallback is not None else []

    def warm(self, names: Iterable[str], *, label: str | None = None) -> dict[str, str]:
        """Fetch each prompt once at boot. Returns ``{name: source}`` so the host
        can log which prompts are being served from where; a name that no layer
        can serve maps to ``"unavailable"`` rather than raising, because boot is
        the wrong moment to crash and the first request will raise anyway."""
        report: dict[str, str] = {}
        for name in names:
            try:
                report[name] = self.get(name, label=label).source
            except Exception:  # noqa: BLE001 - see the docstring: never crash boot
                report[name] = "unavailable"
        return report

    def invalidate(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._cache.clear()
            else:
                for key in [k for k in self._cache if k[0] == name]:
                    del self._cache[key]
