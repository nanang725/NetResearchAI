import pytest

from app.schemas.research_agent import NodeInputBinding
from app.services.research_agent.bindings import (
    BindingResolutionError,
    resolve_node_input,
    resolve_output_path,
)
from app.services.research_agent.tools import TOOL_REGISTRY


def test_resolves_array_binding_and_validates_target_schema():
    resolved = resolve_node_input(
        tool_name="compare_papers",
        node_id="compare",
        original_arguments={
            "topic": "network traffic prediction",
            "include_future_work": True,
        },
        bindings={
            "paper_ids": NodeInputBinding(
                source_node_id="search",
                output_key="result_paper_ids",
                max_items=5,
            )
        },
        dependencies=["search"],
        node_statuses={"search": "completed"},
        completed_outputs={
            "search": {
                "success": True,
                "status": "success",
                "result_paper_ids": [11, 12, 13, 14, 15, 16],
            }
        },
        registry=TOOL_REGISTRY,
    )

    assert resolved.arguments["paper_ids"] == [11, 12, 13, 14, 15]
    assert resolved.binding_sources[0].source_node_id == "search"
    assert resolved.binding_sources[0].item_count == 5


def test_resolves_nested_dict_and_array_paths_without_eval():
    output = {
        "data": {
            "papers": [
                {"paper_id": 7, "metadata": {"year": 2025}},
                {"paper_id": 9, "metadata": {"year": 2024}},
            ]
        }
    }
    assert resolve_output_path(output, "data.papers.1.metadata.year") == 2024
    with pytest.raises(BindingResolutionError) as captured:
        resolve_output_path(output, "__class__.__mro__")
    assert captured.value.code == "AGENT_BINDING_INVALID_PATH"


@pytest.mark.parametrize(
    ("source", "statuses", "dependencies", "outputs", "binding", "expected_code"),
    [
        (
            "missing",
            {},
            ["missing"],
            {},
            NodeInputBinding(source_node_id="missing", output_key="paper_ids"),
            "AGENT_BINDING_UNKNOWN_NODE",
        ),
        (
            "search",
            {"search": "completed"},
            [],
            {"search": {"paper_ids": [1, 2]}},
            NodeInputBinding(source_node_id="search", output_key="paper_ids"),
            "AGENT_BINDING_UNDECLARED_DEPENDENCY",
        ),
        (
            "search",
            {"search": "running"},
            ["search"],
            {"search": {"paper_ids": [1, 2]}},
            NodeInputBinding(source_node_id="search", output_key="paper_ids"),
            "AGENT_BINDING_SOURCE_NOT_COMPLETED",
        ),
        (
            "search",
            {"search": "completed"},
            ["search"],
            {"search": {"other": [1, 2]}},
            NodeInputBinding(source_node_id="search", output_key="paper_ids"),
            "AGENT_BINDING_FIELD_NOT_FOUND",
        ),
    ],
)
def test_rejects_invalid_binding_references(
    source,
    statuses,
    dependencies,
    outputs,
    binding,
    expected_code,
):
    with pytest.raises(BindingResolutionError) as captured:
        resolve_node_input(
            tool_name="compare_papers",
            node_id="compare",
            original_arguments={"topic": "network traffic"},
            bindings={"paper_ids": binding},
            dependencies=dependencies,
            node_statuses=statuses,
            completed_outputs=outputs,
            registry=TOOL_REGISTRY,
        )
    assert captured.value.code == expected_code


def test_binding_cannot_bypass_target_tool_schema():
    with pytest.raises(BindingResolutionError) as captured:
        resolve_node_input(
            tool_name="compare_papers",
            node_id="compare",
            original_arguments={"topic": "network traffic"},
            bindings={
                "paper_ids": NodeInputBinding(
                    source_node_id="search",
                    output_key="paper_ids",
                )
            },
            dependencies=["search"],
            node_statuses={"search": "completed"},
            completed_outputs={"search": {"paper_ids": [1]}},
            registry=TOOL_REGISTRY,
        )
    assert captured.value.code == "AGENT_BINDING_TARGET_SCHEMA_INVALID"


def test_original_argument_and_binding_cannot_conflict():
    with pytest.raises(BindingResolutionError) as captured:
        resolve_node_input(
            tool_name="compare_papers",
            node_id="compare",
            original_arguments={
                "topic": "network traffic",
                "paper_ids": [1, 2],
            },
            bindings={
                "paper_ids": NodeInputBinding(
                    source_node_id="search",
                    output_key="paper_ids",
                )
            },
            dependencies=["search"],
            node_statuses={"search": "completed"},
            completed_outputs={"search": {"paper_ids": [3, 4]}},
            registry=TOOL_REGISTRY,
        )
    assert captured.value.code == "AGENT_BINDING_ARGUMENT_CONFLICT"
