"""Package fixtures. The engine is stateless except for the declared-rules and
known-fields registries — each test declares what it needs; the autouse fixture
resets both so tests never leak into each other."""

import pytest

from asas_validation import configure_clock, declare_rules
from asas_validation import fields as _fields


@pytest.fixture(autouse=True)
def _reset_registries():
    yield
    declare_rules(())
    _fields._FIELDS.clear()
    configure_clock(None)
