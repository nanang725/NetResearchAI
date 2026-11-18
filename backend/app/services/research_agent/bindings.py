from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.schemas.research_agent import NodeInputBinding
from app.services.research_agent.tools import ToolExecutionError, ToolRegistry


_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class BindingResolutionError(Exception):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BindingSource:
    argument: str
    source_node_id: str
    output_key: str
    resolved_type: str
    item_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "argument": self.argument,
            "source_node_id": self.source_node_id,
            "output_key": self.output_key,
            "resolved_type": self.resolved_type,
            "item_count": self.item_count,
        }


@dataclass(frozen=True)
class ResolvedNodeInput:
    arguments: dict[str, Any]
    binding_sources: list[BindingSource]


def _split_path(path: str) -> list[str]:
    segments = path.split(".")
    if (
        not segments
        or len(segments) > 12
        or any(
            not segment
            or segment.startswith("_")
            or not _PATH_SEGMENT.fullmatch(segment)
            for segment in segments
        )
    ):
        raise BindingResolutionError(
            f"输出字段路径 {path!r} 不合法。",
            code="AGENT_BINDING_INVALID_PATH",
        )
    return segments


def resolve_output_path(output: Mapping[str, Any], output_key: str) -> Any:
    current: Any = output
    for segment in _split_path(output_key):
        if isinstance(current, Mapping):
            if segment.startswith("_") or segment not in current:
                raise BindingResolutionError(
                    f"输出中不存在字段 {output_key!r}。",
                    code="AGENT_BINDING_FIELD_NOT_FOUND",
                )
            current = current[segment]
            continue
        if isinstance(current, list):
            if not segment.isdigit():
                raise BindingResolutionError(
                    f"输出字段 {output_key!r} 的数组索引不合法。",
                    code="AGENT_BINDING_INVALID_INDEX",
                )
            index = int(segment)
            if index >= len(current):
                raise BindingResolutionError(
                    f"输出字段 {output_key!r} 的数组索引 {index} 越界。",
                    code="AGENT_BINDING_INDEX_OUT_OF_RANGE",
                )
            current = current[index]
            continue
        raise BindingResolutionError(
            f"输出字段 {output_key!r} 无法继续解析。",
            code="AGENT_BINDING_FIELD_NOT_FOUND",
        )
    return current


def resolve_node_input(
    *,
    tool_name: str,
    node_id: str,
    original_arguments: dict[str, Any],
    bindings: dict[str, NodeInputBinding],
    dependencies: list[str],
    node_statuses: Mapping[str, str],
    completed_outputs: Mapping[str, Mapping[str, Any]],
    registry: ToolRegistry,
) -> ResolvedNodeInput:
    final_arguments = json.loads(json.dumps(original_arguments))
    sources: list[BindingSource] = []
    dependency_set = set(dependencies)

    for argument_name, binding in bindings.items():
        if not _PATH_SEGMENT.fullmatch(argument_name):
            raise BindingResolutionError(
                f"绑定目标参数 {argument_name!r} 不合法。",
                code="AGENT_BINDING_INVALID_ARGUMENT",
            )
        if argument_name in original_arguments:
            raise BindingResolutionError(
                f"参数 {argument_name!r} 同时存在原始值和数据绑定。",
                code="AGENT_BINDING_ARGUMENT_CONFLICT",
            )
        source_id = binding.source_node_id
        if source_id not in node_statuses:
            raise BindingResolutionError(
                f"绑定的来源节点 {source_id!r} 不存在。",
                code="AGENT_BINDING_UNKNOWN_NODE",
            )
        if source_id not in dependency_set:
            raise BindingResolutionError(
                f"节点 {node_id} 未声明对绑定来源 {source_id} 的依赖。",
                code="AGENT_BINDING_UNDECLARED_DEPENDENCY",
            )
        if node_statuses[source_id] != "completed":
            raise BindingResolutionError(
                f"绑定来源节点 {source_id!r} 尚未成功完成。",
                code="AGENT_BINDING_SOURCE_NOT_COMPLETED",
            )
        output = completed_outputs.get(source_id)
        if output is None:
            raise BindingResolutionError(
                f"绑定来源节点 {source_id!r} 没有持久化结构化输出。",
                code="AGENT_BINDING_OUTPUT_NOT_FOUND",
            )
        value = resolve_output_path(output, binding.output_key)
        if binding.max_items is not None:
            if not isinstance(value, list):
                raise BindingResolutionError(
                    f"字段 {binding.output_key!r} 不是数组，不能应用 max_items。",
                    code="AGENT_BINDING_MAX_ITEMS_NOT_ARRAY",
                )
            value = value[: binding.max_items]
        final_arguments[argument_name] = value
        sources.append(
            BindingSource(
                argument=argument_name,
                source_node_id=source_id,
                output_key=binding.output_key,
                resolved_type=type(value).__name__,
                item_count=len(value) if isinstance(value, list) else None,
            )
        )

    try:
        registry.validate_arguments(tool_name, final_arguments)
    except ToolExecutionError as exc:
        raise BindingResolutionError(
            f"绑定后的工具参数未通过 Schema 校验：{exc}",
            code="AGENT_BINDING_TARGET_SCHEMA_INVALID",
        ) from exc
    return ResolvedNodeInput(arguments=final_arguments, binding_sources=sources)
