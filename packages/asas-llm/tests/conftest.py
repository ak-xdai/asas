"""Standalone package fixtures. No database, no network, no model: the suite
runs on a fake chat model and an in-memory tracer, and the Langfuse adapter is
tested against a stub client. Both context variables leak between tests
otherwise, hence the autouse reset."""

from __future__ import annotations

import asyncio

import pytest

import asas_llm
from asas_llm import context


@pytest.fixture(autouse=True)
def _clean_context():
    context.clear_request()
    asas_llm.set_runner(None)
    asas_llm._settings = None          # configure() state must not leak either:
    yield                              # runner() lazily rebuilds from it, so a
    context.clear_request()            # stale value makes the NotConfigured
    asas_llm.set_runner(None)          # contract test order-dependent.
    asas_llm._settings = None


def run(coro):
    """Drive a coroutine from a sync test without an async plugin."""
    return asyncio.run(coro)
