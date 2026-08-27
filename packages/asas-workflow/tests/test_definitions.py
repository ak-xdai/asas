"""Definition validation — the checks that turn a malformed process into a
boot-time error instead of a runtime one."""

import pytest

from asas_workflow import (
    DefinitionSpec,
    NodeSpec,
    NodeType,
    TransitionSpec,
    validate_definition,
)




def test_end_node_without_outcome_is_rejected_at_validation():
    """Pins a check that already existed and had no test.

    The engine reads `config["outcome"]` unguarded when an instance reaches an
    end node, so without validation it would be a bare KeyError from inside the
    engine at decision time. `validate_definition` has always caught it — that
    was verified the hard way, by adding a second copy of the check and having
    review point out the duplicate. The test is what was missing.
    """
    spec = DefinitionSpec(
        key="no_outcome",
        name="No outcome",
        entity_type="thing",
        nodes=(
            NodeSpec("start", "Start", NodeType.start),
            NodeSpec("done", "Done", NodeType.end),  # no config
        ),
        transitions=(TransitionSpec("start", "done"),),
    )
    with pytest.raises(ValueError, match="needs an outcome"):
        validate_definition(spec)


def test_end_node_with_outcome_validates():
    spec = DefinitionSpec(
        key="with_outcome",
        name="With outcome",
        entity_type="thing",
        nodes=(
            NodeSpec("start", "Start", NodeType.start),
            NodeSpec("done", "Done", NodeType.end, {"outcome": "approved"}),
        ),
        transitions=(TransitionSpec("start", "done"),),
    )
    validate_definition(spec)  # does not raise
