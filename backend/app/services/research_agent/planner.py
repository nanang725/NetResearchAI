from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.services.llm_json import (
    LLMJSONError,
    classify_llm_exception,
    finish_reason,
    parse_json_object,
    repair_messages,
    response_content,
    safe_diagnostics,
)
from app.schemas.research_agent import (
    AgentBudgetRequest,
    PlannedNode,
    PlannerAnalysis,
    PlannerPlan,
    ReplanOutput,
    ValidatedPlan,
)
from app.services.llm_service import get_llm_client
from app.services.research_agent.tools import (
    TOOL_REGISTRY,
    ToolExecutionError,
    ToolRegistry,
)


class PlanValidationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "AGENT_PLAN_INVALID",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _schema_placeholder(schema: dict[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "anyOf" in schema:
        candidates = [
            item
            for item in schema["anyOf"]
            if item.get("type") != "null"
        ]
        return _schema_placeholder(candidates[0]) if candidates else None
    schema_type = schema.get("type")
    if schema_type == "array":
        minimum = max(1, int(schema.get("minItems", 0)))
        item_schema = schema.get("items", {})
        placeholder = _schema_placeholder(item_schema)
        return [
            placeholder + index if isinstance(placeholder, int) else placeholder
            for index in range(minimum)
        ]
    if schema_type == "integer":
        return max(1, int(schema.get("minimum", 1)))
    if schema_type == "number":
        return max(1.0, float(schema.get("minimum", 1.0)))
    if schema_type == "boolean":
        return False
    if schema_type == "object":
        return {}
    minimum_length = max(1, int(schema.get("minLength", 1)))
    return "x" * minimum_length


def _validated_parameters(node: PlannedNode, registry: ToolRegistry) -> None:
    spec = registry.get(node.tool_name)
    candidate_arguments = dict(node.arguments)
    properties = spec.input_schema.model_json_schema().get("properties", {})
    for target, binding in node.input_bindings.items():
        if target not in properties:
            raise PlanValidationError(
                f"节点 {node.id} 的数据绑定目标 {target!r} 不是工具输入参数。",
                code="AGENT_PLAN_UNKNOWN_BINDING_TARGET",
                details={"node_id": node.id, "target": target},
            )
        if target in candidate_arguments:
            raise PlanValidationError(
                f"节点 {node.id} 的参数 {target!r} 同时存在静态值和数据绑定。",
                code="AGENT_PLAN_BINDING_ARGUMENT_CONFLICT",
                details={"node_id": node.id, "target": target},
            )
        candidate_arguments[target] = _schema_placeholder(properties[target])
    try:
        registry.validate_arguments(node.tool_name, candidate_arguments)
        if node.failure_strategy == "fallback_tool" and node.fallback_tool:
            fallback_spec = registry.get(node.fallback_tool)
            if fallback_spec.side_effect != "read_only":
                raise ToolExecutionError(
                    "替代工具必须是只读工具。",
                    code="AGENT_PLAN_WRITE_TOOL_FORBIDDEN",
                    retryable=False,
                )
            registry.validate_arguments(
                node.fallback_tool,
                node.fallback_arguments,
            )
    except ToolExecutionError as exc:
        raise PlanValidationError(
            f"节点 {node.id} 的参数不合法。",
            code="AGENT_PLAN_INVALID_PARAMETERS",
            details={"node_id": node.id, "cause": exc.code},
        ) from exc


def _validate_dag(
    nodes: list[PlannedNode],
    existing_node_ids: set[str],
) -> None:
    node_ids = {node.id for node in nodes}
    adjacency: dict[str, list[str]] = {node.id: [] for node in nodes}
    incoming: Counter[str] = Counter()
    for node in nodes:
        for dependency in node.depends_on:
            if dependency == node.id:
                raise PlanValidationError(
                    f"节点 {node.id} 不能依赖自身。",
                    code="AGENT_PLAN_SELF_DEPENDENCY",
                )
            if dependency not in node_ids and dependency not in existing_node_ids:
                raise PlanValidationError(
                    f"节点 {node.id} 依赖不存在的节点 {dependency}。",
                    code="AGENT_PLAN_UNKNOWN_DEPENDENCY",
                )
            if dependency in node_ids:
                adjacency[dependency].append(node.id)
                incoming[node.id] += 1

    ready = [node_id for node_id in node_ids if incoming[node_id] == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in adjacency[current]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(node_ids):
        raise PlanValidationError(
            "计划包含循环依赖。",
            code="AGENT_PLAN_CYCLE",
        )
    for node in nodes:
        for input_name, binding in node.input_bindings.items():
            if binding.source_node_id not in node_ids and binding.source_node_id not in existing_node_ids:
                raise PlanValidationError(
                    f"节点 {node.id} 的输入 {input_name} 引用了不存在的节点。",
                    code="AGENT_PLAN_UNKNOWN_INPUT_SOURCE",
                )
            if binding.source_node_id not in node.depends_on:
                raise PlanValidationError(
                    f"节点 {node.id} 的输入 {input_name} 必须同时声明依赖。",
                    code="AGENT_PLAN_INPUT_DEPENDENCY_REQUIRED",
                )


def validate_plan(
    plan: PlannerPlan,
    budget: AgentBudgetRequest,
    *,
    version: int,
    generated_by: str = "llm",
    registry: ToolRegistry = TOOL_REGISTRY,
    existing_nodes: list[PlannedNode] | None = None,
    edited_by_user: bool = False,
) -> ValidatedPlan:
    existing_nodes = existing_nodes or []
    existing_ids = {node.id for node in existing_nodes}
    all_nodes = [*existing_nodes, *plan.nodes]
    if len(all_nodes) > budget.max_nodes:
        raise PlanValidationError(
            f"计划节点数 {len(all_nodes)} 超过预算 {budget.max_nodes}。",
            code="AGENT_PLAN_NODE_BUDGET_EXCEEDED",
        )
    node_ids = [node.id for node in plan.nodes]
    if len(node_ids) != len(set(node_ids)):
        raise PlanValidationError(
            "计划节点 ID 必须唯一。",
            code="AGENT_PLAN_DUPLICATE_NODE_ID",
        )
    if existing_ids.intersection(node_ids):
        raise PlanValidationError(
            "重规划不能覆盖已持久化节点。",
            code="AGENT_PLAN_REPLAN_CONFLICT",
        )

    tool_counts = Counter(node.tool_name for node in all_nodes)
    for node in all_nodes:
        if node.failure_strategy == "fallback_tool" and node.fallback_tool:
            tool_counts[node.fallback_tool] += 1
    fallback_call_count = sum(
        node.failure_strategy == "fallback_tool"
        and node.fallback_tool is not None
        for node in all_nodes
    )
    arxiv_count = tool_counts.get("search_arxiv", 0)
    if arxiv_count > budget.max_arxiv_requests:
        raise PlanValidationError(
            f"计划中的 arXiv 请求数 {arxiv_count} 超过预算。",
            code="AGENT_PLAN_ARXIV_BUDGET_EXCEEDED",
        )
    if len(all_nodes) + fallback_call_count > budget.max_tool_calls:
        raise PlanValidationError(
            "计划最坏工具调用数 "
            f"{len(all_nodes) + fallback_call_count} 超过预算。",
            code="AGENT_PLAN_CALL_BUDGET_EXCEEDED",
        )
    llm_calls = 0
    for node in plan.nodes:
        try:
            spec = registry.get(node.tool_name)
        except ToolExecutionError as exc:
            raise PlanValidationError(
                f"计划引用了未注册工具 {node.tool_name!r}。",
                code="AGENT_PLAN_UNREGISTERED_TOOL",
                details={"node_id": node.id, "tool_name": node.tool_name},
            ) from exc
        if spec.side_effect != "read_only":
            raise PlanValidationError(
                "自主计划只能包含只读工具。",
                code="AGENT_PLAN_WRITE_TOOL_FORBIDDEN",
            )
        llm_calls += spec.llm_calls_per_execution
        if node.failure_strategy == "fallback_tool" and node.fallback_tool:
            try:
                fallback_spec = registry.get(node.fallback_tool)
            except ToolExecutionError as exc:
                raise PlanValidationError(
                    f"计划引用了未注册替代工具 {node.fallback_tool!r}。",
                    code="AGENT_PLAN_UNREGISTERED_TOOL",
                    details={
                        "node_id": node.id,
                        "fallback_tool": node.fallback_tool,
                    },
                ) from exc
            if fallback_spec.side_effect != "read_only":
                raise PlanValidationError(
                    "计划中的替代工具必须是只读工具。",
                    code="AGENT_PLAN_WRITE_TOOL_FORBIDDEN",
                )
            llm_calls += fallback_spec.llm_calls_per_execution
        _validated_parameters(node, registry)
    for tool_name, count in tool_counts.items():
        spec = registry.get(tool_name)
        if count > spec.max_calls_per_run:
            raise PlanValidationError(
                f"工具 {tool_name} 的计划调用数超过单任务上限。",
                code="AGENT_PLAN_TOOL_LIMIT_EXCEEDED",
                details={
                    "tool_name": tool_name,
                    "count": count,
                    "max_calls_per_run": spec.max_calls_per_run,
                },
            )
    if llm_calls > budget.max_llm_calls:
        raise PlanValidationError(
            f"工具隐含 LLM 调用数 {llm_calls} 超过预算。",
            code="AGENT_PLAN_LLM_BUDGET_EXCEEDED",
        )
    _validate_dag(plan.nodes, existing_ids)
    return ValidatedPlan(
        version=version,
        topic=plan.analysis.topic,
        research_question=plan.analysis.research_question,
        time_range=plan.analysis.time_range,
        analysis_dimensions=plan.analysis.analysis_dimensions,
        nodes=all_nodes,
        generated_by=generated_by,
        edited_by_user=edited_by_user,
    )


def _fallback_analysis(goal: str) -> PlannerAnalysis:
    topic = re.sub(r"^(请|帮我|研究|分析)+", "", goal).strip()[:200]
    return PlannerAnalysis(
        topic=topic or goal[:200],
        research_question=(
            f"围绕“{topic or goal[:160]}”有哪些有可核验来源支持的研究发现、"
            "方法差异与研究空白？"
        ),
        time_range=None,
        analysis_dimensions=["研究问题", "方法", "结果", "局限"],
    )


def fallback_plan(
    goal: str,
    budget: AgentBudgetRequest | None = None,
) -> PlannerPlan:
    budget = budget or AgentBudgetRequest()
    analysis = _fallback_analysis(goal)
    candidates: list[PlannedNode] = [
        PlannedNode(
            id="search_local",
            tool_name="search_local_papers",
            arguments={"query": analysis.topic, "limit": 6},
            depends_on=[],
            reason="使用现有混合检索寻找可定位页码和 Chunk 的本地全文证据。",
            completion_condition="返回本地全文证据及对应论文 ID。",
            retry_limit=1,
        )
    ]
    # The planner reserves one call for comparison (including one repair),
    # one call for the final report, and one call for plan generation.
    if budget.max_llm_calls >= 4:
        candidates.append(
            PlannedNode(
                id="compare_local",
                tool_name="compare_papers",
                arguments={
                    "topic": analysis.topic,
                    "include_future_work": True,
                },
                depends_on=["search_local"],
                reason=(
                    "将全库检索得到的真实论文 ID 自动绑定到多论文比较工具，"
                    "避免手工填写或推测论文范围。"
                ),
                completion_condition="对命中的 2 至 5 篇真实本地论文完成证据比较。",
                retry_limit=0,
                input_bindings={
                    "paper_ids": {
                        "source_node_id": "search_local",
                        "output_key": "result_paper_ids",
                        "max_items": 5,
                    }
                },
            )
        )
        candidates.append(
            PlannedNode(
                id="draft_outline",
                tool_name="generate_research_outline",
                arguments={"topic": analysis.topic},
                depends_on=["compare_local"],
                reason="读取比较结果中的首篇真实本地论文 ID，生成可回溯证据大纲。",
                completion_condition="为比较命中论文生成带真实 Chunk 的证据大纲。",
                retry_limit=0,
                input_bindings={
                    "paper_id": {
                        "source_node_id": "compare_local",
                        "output_key": "selected_paper_ids.0",
                    }
                },
            )
        )
    candidates.extend(
        [
            PlannedNode(
                id="paper_structure",
                tool_name="get_paper_structure",
                arguments={
                    "max_pages": 5,
                    "max_elements_per_page": 10,
                    "include_text": True,
                },
                depends_on=["search_local"],
                reason="读取首篇真实命中论文的页面和结构化元素。",
                completion_condition="返回真实页面、章节和 Chunk 关联。",
                retry_limit=0,
                input_bindings={
                    "paper_id": {
                        "source_node_id": "search_local",
                        "output_key": "result_paper_ids.0",
                    }
                },
            ),
            PlannedNode(
                id="inspect_library",
                tool_name="inspect_local_library",
                arguments={"query": "", "limit": 20},
                depends_on=[],
                reason="先确认本地论文库规模与可用状态，避免在空库上执行无效检索。",
                completion_condition="获得本地论文总数及可用论文元数据。",
                retry_limit=0,
            ),
            PlannedNode(
                id="verify_references",
                tool_name="get_verifiable_references",
                arguments={"paper_ids": []},
                depends_on=["inspect_library"],
                reason="读取现有 References 服务中可关联本地正文的引用元数据。",
                completion_condition="返回可核验或明确标记不可核验的参考文献。",
                retry_limit=0,
            ),
            PlannedNode(
                id="inspect_citation_graph",
                tool_name="get_citation_graph",
                arguments={"limit": 50, "min_citations": 0},
                depends_on=["inspect_library"],
                reason="读取现有本地引用图，帮助识别当前语料中的核心论文。",
                completion_condition="返回引用图节点与统计，不生成不存在的关系。",
                retry_limit=0,
            ),
            PlannedNode(
                id="inspect_knowledge_graph",
                tool_name="explore_knowledge_graph",
                arguments={"query": "", "limit": 100},
                depends_on=["search_local"],
                reason=(
                    "读取首篇真实命中论文周边的方法、数据集、结论与跨论文关系，"
                    "只接受带页码、Chunk 和证据文本的图谱边。"
                ),
                completion_condition="返回可定位证据的科研实体与关系子图。",
                retry_limit=0,
                input_bindings={
                    "paper_id": {
                        "source_node_id": "search_local",
                        "output_key": "result_paper_ids.0",
                    }
                },
            ),
        ]
    )
    limit = min(budget.max_nodes, budget.max_tool_calls)
    nodes = candidates[:limit]
    allowed_ids = {node.id for node in nodes}
    dependent_nodes = []
    for node in nodes:
        dependencies = [
            dependency
            for dependency in node.depends_on
            if dependency in allowed_ids
        ]
        bindings = {
            argument: binding
            for argument, binding in node.input_bindings.items()
            if binding.source_node_id in allowed_ids
        }
        if len(bindings) != len(node.input_bindings):
            continue
        dependent_nodes.append(
            node.model_copy(
                update={
                    "depends_on": dependencies,
                    "input_bindings": bindings,
                }
            )
        )
    return PlannerPlan(analysis=analysis, nodes=dependent_nodes)


def _extract_json(content: str) -> dict[str, Any]:
    return parse_json_object(content)


def _normalize_binding_dependencies(plan: PlannerPlan) -> PlannerPlan:
    nodes = []
    for node in plan.nodes:
        arguments = {
            name: value
            for name, value in node.arguments.items()
            if name not in node.input_bindings
        }
        dependencies = list(dict.fromkeys(
            [
                *node.depends_on,
                *(
                    binding.source_node_id
                    for binding in node.input_bindings.values()
                ),
            ]
        ))
        nodes.append(
            node.model_copy(
                update={
                    "depends_on": dependencies,
                    "arguments": arguments,
                }
            )
        )
    return plan.model_copy(update={"nodes": nodes})


class ResearchPlanner:
    def __init__(
        self,
        *,
        registry: ToolRegistry = TOOL_REGISTRY,
    ) -> None:
        self.registry = registry
        self.last_llm_calls_used = 0
        self.last_llm_diagnostics: dict[str, Any] | None = None
        self.last_raw_content: str | None = None

    def _tool_catalog(self) -> str:
        return json.dumps(
            [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema.model_json_schema(),
                    "side_effect": spec.side_effect,
                    "max_calls_per_run": spec.max_calls_per_run,
                }
                for spec in self.registry.specs()
            ],
            ensure_ascii=False,
        )

    def create_plan(
        self,
        goal: str,
        budget: AgentBudgetRequest,
    ) -> ValidatedPlan:
        self.last_llm_calls_used = 0
        self.last_llm_diagnostics = None
        self.last_raw_content = None
        if budget.max_llm_calls <= 0:
            return validate_plan(
                fallback_plan(goal, budget),
                budget,
                version=1,
                generated_by="fallback",
                registry=self.registry,
            )
        response = None
        content: str | None = None
        try:
            settings = get_settings()
            client = get_llm_client()
            response = client.chat.completions.create(
                model=settings.llm_model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是科研 Agent 的结构化 Planner。只能从给定工具白名单"
                            "中选择工具。不得输出 Python、SQL、函数名或 API 路径。"
                            "只返回合法 JSON。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"研究目标：{goal}\n\n"
                            f"预算：最多 {budget.max_nodes} 个节点、"
                            f"{budget.max_tool_calls} 次工具调用、"
                            f"{budget.max_arxiv_requests} 次 arXiv 请求。\n\n"
                            f"工具目录：{self._tool_catalog()}\n\n"
                            "请识别主题、核心研究问题、时间范围和分析维度，并生成"
                            "具有明确依赖的 DAG。节点 arguments 必须符合对应 Schema。"
                            "需要前序输出时使用 input_bindings，格式为"
                            '{"目标参数":{"source_node_id":"前序节点",'
                            '"output_key":"data.字段或数组.索引","max_items":5}}；'
                            "source_node_id 必须同时出现在 depends_on。"
                            "多论文比较应从 search_local_papers 的 result_paper_ids 绑定"
                            "最多 5 篇真实论文；后续大纲可从比较结果的 "
                            "selected_paper_ids.0 绑定真实 paper_id。"
                            "每个节点必须说明执行理由和完成条件。优先使用本地全文，"
                            "仅在需要时使用 arXiv。输出结构："
                            '{"analysis":{"topic":"...","research_question":"...",'
                            '"time_range":null,"analysis_dimensions":["..."]},'
                            '"nodes":[{"id":"node_id","tool_name":"...",'
                            '"arguments":{},"input_bindings":{},"depends_on":[],"reason":"...",'
                            '"completion_condition":"...","retry_limit":1}]}'
                        ),
                    },
                ],
                temperature=0,
                response_format={"type": "json_object"},
                timeout=60,
            )
            self.last_llm_calls_used = 1
            content = response_content(response)
            self.last_raw_content = content
            plan = _normalize_binding_dependencies(
                PlannerPlan.model_validate(_extract_json(content))
            )
            return validate_plan(
                plan,
                budget,
                version=1,
                generated_by="llm",
                registry=self.registry,
            )
        except Exception as exc:
            if isinstance(exc, PlanValidationError):
                self.last_llm_diagnostics = {
                    "stage": "planner",
                    "attempts": [
                        {
                            "stage": "planner",
                            "attempt": self.last_llm_calls_used or 1,
                            "failure_kind": "plan_validation_error",
                            "error_type": type(exc).__name__,
                            "error_code": getattr(exc, "code", None),
                            "error": str(exc),
                        }
                    ],
                    "fallback_used": True,
                }
                return validate_plan(
                    fallback_plan(goal, budget),
                    budget,
                    version=1,
                    generated_by="fallback",
                    registry=self.registry,
                )
            first_failure_kind = (
                exc.kind
                if isinstance(exc, LLMJSONError)
                else (
                    "schema_validation_error"
                    if isinstance(exc, ValidationError)
                    else classify_llm_exception(exc)
                )
            )
            first_diagnostic = safe_diagnostics(
                stage="planner",
                attempt=self.last_llm_calls_used or 1,
                content=self.last_raw_content or content,
                finish_reason=finish_reason(response),
                failure_kind=first_failure_kind,
                error=exc,
            )
            if (
                isinstance(exc, (LLMJSONError, ValidationError))
                and content
                and budget.max_llm_calls >= 4
            ):
                try:
                    self.last_llm_calls_used = 2
                    repair_response = client.chat.completions.create(
                        model=settings.llm_model_name,
                        messages=repair_messages(
                            invalid_content=content,
                            schema_text=json.dumps(
                                PlannerPlan.model_json_schema(),
                                ensure_ascii=False,
                            ),
                            instruction=(
                                "Preserve the requested research goal and "
                                "every existing node. Keep only registered "
                                "tool names and valid binding sources."
                            ),
                        ),
                        temperature=0,
                        response_format={"type": "json_object"},
                        timeout=30,
                    )
                    response = repair_response
                    repaired_content = response_content(repair_response)
                    self.last_raw_content = repaired_content
                    repaired_plan = _normalize_binding_dependencies(
                        PlannerPlan.model_validate(
                            _extract_json(repaired_content)
                        )
                    )
                    validated = validate_plan(
                        repaired_plan,
                        budget,
                        version=1,
                        generated_by="llm",
                        registry=self.registry,
                    )
                    self.last_llm_diagnostics = {
                        "stage": "planner",
                        "attempts": [
                            first_diagnostic,
                            safe_diagnostics(
                                stage="planner",
                                attempt=2,
                                content=repaired_content,
                                finish_reason=finish_reason(response),
                                failure_kind="repair_succeeded",
                            ),
                        ],
                        "repaired": True,
                    }
                    return validated
                except Exception as repair_exc:
                    self.last_llm_diagnostics = {
                        "stage": "planner",
                        "attempts": [
                            first_diagnostic,
                            safe_diagnostics(
                                stage="planner",
                                attempt=2,
                                content=self.last_raw_content,
                                finish_reason=finish_reason(response),
                                failure_kind=(
                                    repair_exc.kind
                                    if isinstance(repair_exc, LLMJSONError)
                                    else "schema_error"
                                ),
                                error=repair_exc,
                            ),
                        ],
                        "repaired": False,
                    }
                    exc = repair_exc
            failure_kind = (
                exc.kind
                if isinstance(exc, LLMJSONError)
                else (
                    "schema_validation_error"
                    if isinstance(exc, ValidationError)
                    else classify_llm_exception(exc)
                )
            )
            if self.last_llm_diagnostics is None:
                self.last_llm_diagnostics = safe_diagnostics(
                    stage="planner",
                    attempt=self.last_llm_calls_used or 1,
                    content=self.last_raw_content or content,
                    finish_reason=finish_reason(response),
                    failure_kind=failure_kind,
                    error=exc,
                )
            fallback = fallback_plan(goal, budget)
            return validate_plan(
                fallback,
                budget,
                version=1,
                generated_by="fallback",
                registry=self.registry,
            )

    def controlled_replan(
        self,
        existing: ValidatedPlan,
        *,
        budget: AgentBudgetRequest,
        source_node_id: str,
        result: dict[str, Any],
    ) -> ValidatedPlan | None:
        existing_tools = {node.tool_name for node in existing.nodes}
        result_count = int(result.get("result_count") or 0)
        paper_ids = [
            int(item)
            for item in result.get("result_paper_ids", [])
            if isinstance(item, int) and item > 0
        ]
        if result_count < 2 and "search_arxiv" not in existing_tools:
            nodes = [
                PlannedNode(
                    id="dynamic_external_search",
                    tool_name="search_arxiv",
                    arguments={
                        "query": existing.topic,
                        "field": "all",
                        "page": 1,
                        "page_size": 5,
                        "sort_by": "relevance",
                        "sort_order": "descending",
                    },
                    depends_on=[source_node_id],
                    reason=(
                        f"本地检索仅返回 {result_count} 条全文证据，按运行时条件"
                        "追加一次受控 arXiv 摘要检索。"
                    ),
                    completion_condition="获得外部题录与摘要，或明确记录外部搜索失败。",
                    retry_limit=1,
                )
            ]
            replan_reason = "本地全文证据不足，追加外部摘要搜索。"
        elif result_count >= 2 and paper_ids and "generate_research_outline" not in existing_tools:
            nodes = [
                PlannedNode(
                    id="dynamic_outline",
                    tool_name="generate_research_outline",
                    arguments={
                        "paper_id": paper_ids[0],
                        "topic": existing.topic[:200],
                    },
                    depends_on=[source_node_id],
                    reason=(
                        f"本地检索已获得 {len(paper_ids)} 篇论文的证据，"
                        f"针对 paper_id={paper_ids[0]} 追加证据化大纲。"
                    ),
                    completion_condition="获得与真实 Chunk 关联的大纲证据。",
                    retry_limit=0,
                )
            ]
            replan_reason = "本地证据达到阈值，针对实际命中的论文追加证据大纲。"
        else:
            return None

        plan = PlannerPlan(
            analysis=PlannerAnalysis(
                topic=existing.topic,
                research_question=existing.research_question,
                time_range=existing.time_range,
                analysis_dimensions=existing.analysis_dimensions,
            ),
            nodes=nodes,
        )
        try:
            return validate_plan(
                plan,
                budget,
                version=existing.version + 1,
                generated_by="controlled_replan",
                registry=self.registry,
                existing_nodes=existing.nodes,
            )
        except PlanValidationError:
            return None

    @staticmethod
    def replan_reason(
        existing: ValidatedPlan,
        *,
        source_node_id: str,
        result: dict[str, Any],
    ) -> str:
        result_count = int(result.get("result_count") or 0)
        if result_count < 2:
            return (
                f"节点 {source_node_id} 仅返回 {result_count} 条全文证据，"
                "触发一次外部摘要检索。"
            )
        return (
            f"节点 {source_node_id} 返回 {result_count} 条全文证据，"
            "基于真实命中论文追加证据大纲。"
        )
