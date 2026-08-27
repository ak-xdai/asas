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
    """The engine reads config["outcome"] unguarded when an instance ends here.

    Omitting it used to surface as a bare KeyError from inside the engine, at
    decision time, and only for the branch that happened to reach that end node.
    Validation turns it into a boot-time error naming the node.
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
    with pytest.raises(ValueError, match="needs config\\['outcome'\\]"):
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
