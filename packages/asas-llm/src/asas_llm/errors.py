"""Typed failures, each carrying the trace id of the call that failed.

The engine this package was extracted from swallowed every exception inside the
runner and returned ``None``. Every caller then had to remember to check for
``None``, most did, some did not, and the one thing an operator needed to find
the failure afterwards (the trace id) was never in the log line or the error.
Here a failure is an exception, it names the trace it belongs to, and a caller
that wants "try the next area" writes ``except LLMError`` and says so.
"""

from __future__ import annotations

from typing import Any


class LLMError(Exception):
    """Base of every failure this package raises.

    ``trace_id`` is the id of the trace the failing call ran under (``None``
    when it failed before a trace existed, e.g. the prompt could not be
    loaded). ``prompt`` is the prompt name when one is known.
    """

    def __init__(
        self,
        message: str,
        *,
        trace_id: str | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(message)
        self.trace_id = trace_id
        self.prompt = prompt

    def __str__(self) -> str:  # pragma: no cover - formatting only
        base = super().__str__()
        bits = []
        if self.prompt:
            bits.append(f"prompt={self.prompt!r}")
        if self.trace_id:
            bits.append(f"trace_id={self.trace_id}")
        return f"{base} ({', '.join(bits)})" if bits else base


class NotConfigured(LLMError):
    """``asas_llm.runner()`` was called before ``configure()``."""


class PromptUnavailable(LLMError):
    """No source could serve the prompt: the registry was unreachable or has no
    such prompt, the last-good copy is empty, and the local fallback has no
    file for it. The message names every source that was tried."""


class PromptFormatError(LLMError):
    """A local prompt file exists but cannot be read as a prompt."""


class GenerationFailed(LLMError):
    """The model provider raised. The original exception is ``__cause__``."""


class EmptyOutput(LLMError):
    """The model returned nothing usable (no text at all)."""


class StructuredOutputError(LLMError):
    """The model's text could not be turned into the requested model, even after
    repair. ``raw`` holds the (truncated) text so the failure can be inspected
    without re-running the call; ``errors`` is pydantic's error list when
    validation was what failed."""

    RAW_LIMIT = 4000

    def __init__(
        self,
        message: str,
        *,
        raw: str,
        errors: Any = None,
        trace_id: str | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(message, trace_id=trace_id, prompt=prompt)
        self.raw = raw[: self.RAW_LIMIT]
        self.errors = errors
