from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_nodes: int = Field(default=8, ge=1, le=8)
    max_tool_calls: int = Field(default=12, ge=1, le=12)
    max_llm_calls: int = Field(default=4, ge=0, le=4)
    max_arxiv_requests: int = Field(default=3, ge=0, le=3)
    max_replans: int = Field(default=1, ge=0, le=3)
    max_execution_seconds: int = Field(default=180, ge=10, le=600)


class ResearchAgentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=5, max_length=2000)
    budget: AgentBudgetRequest = Field(default_factory=AgentBudgetRequest)

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("研究目标至少需要 5 个有效字符。")
        return normalized


class ResearchAgentPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=5, max_length=2000)
    topic: str = Field(min_length=1, max_length=200)
    research_question: str = Field(min_length=1, max_length=1000)
    time_range: str | None = Field(default=None, max_length=255)
    analysis_dimensions: list[str] = Field(default_factory=list, max_length=8)
    nodes: list["PlannedNode"] = Field(min_length=1, max_length=8)
    budget: AgentBudgetRequest = Field(default_factory=AgentBudgetRequest)
    change_reason: str = Field(
        default="用户调整研究计划",
        min_length=1,
        max_length=500,
    )

    @field_validator("goal")
    @classmethod
    def validate_plan_goal(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("研究目标至少需要 5 个有效字符。")
        return normalized


class NodeInputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    output_key: str = Field(min_length=1, max_length=120)
    max_items: int | None = Field(default=None, ge=1, le=50)

    @field_validator("output_key")
    @classmethod
    def validate_output_key(cls, value: str) -> str:
        segments = value.split(".")
        if any(
            not segment
            or segment.startswith("_")
            or not segment.isascii()
            or not all(
                character.isalnum() or character in {"_", "-"}
                for character in segment
            )
            for segment in segments
        ):
            raise ValueError("output_key 只能使用受限的字段和数组索引路径。")
        return value


class NodeOutputDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", max_length=1000)
    fields: list[str] = Field(default_factory=list, max_length=30)


class PlannedNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    tool_name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=1000)
    completion_condition: str = Field(min_length=1, max_length=1000)
    retry_limit: int = Field(default=1, ge=0, le=2)
    failure_strategy: Literal[
        "retry_then_fail",
        "skip",
        "fallback_tool",
    ] = "retry_then_fail"
    critical: bool = True
    fallback_tool: str | None = Field(default=None, max_length=80)
    fallback_arguments: dict[str, Any] = Field(default_factory=dict)
    input_bindings: dict[str, NodeInputBinding] = Field(default_factory=dict)
    output_definition: NodeOutputDefinition = Field(
        default_factory=NodeOutputDefinition,
    )

    @model_validator(mode="after")
    def validate_failure_strategy(self) -> "PlannedNode":
        if self.failure_strategy == "skip" and self.critical:
            raise ValueError("关键节点不能使用 skip 失败策略。")
        if self.failure_strategy == "fallback_tool" and not self.fallback_tool:
            raise ValueError("fallback_tool 策略必须指定替代工具。")
        return self


class PlannerAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    research_question: str = Field(min_length=1, max_length=1000)
    time_range: str | None = Field(default=None, max_length=255)
    analysis_dimensions: list[str] = Field(default_factory=list, max_length=8)


class PlannerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: PlannerAnalysis
    nodes: list[PlannedNode] = Field(min_length=1, max_length=8)


class ReplanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    nodes: list[PlannedNode] = Field(min_length=1, max_length=8)


class ValidatedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    topic: str
    research_question: str
    time_range: str | None = None
    analysis_dimensions: list[str] = Field(default_factory=list)
    nodes: list[PlannedNode]
    generated_by: Literal["llm", "fallback", "controlled_replan"]
    edited_by_user: bool = False


class LocalPaperMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: int = Field(gt=0)
    title: str
    authors: str | None = None
    publication_year: int | None = None
    parse_status: str
    index_status: str


class InspectLocalLibraryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=300)
    limit: int = Field(default=20, ge=1, le=100)


class InspectLocalLibraryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_count: int = Field(ge=0)
    result_count: int = Field(ge=0)
    papers: list[LocalPaperMetadata]


class SearchLocalPapersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)
    paper_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("paper_ids")
    @classmethod
    def validate_paper_ids(cls, value: list[int]) -> list[int]:
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in value
        ):
            raise ValueError("paper_ids 只能包含正整数。")
        return list(dict.fromkeys(value))


class SearchArxivInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=300)
    field: Literal["all", "title", "abstract", "author"] = "all"
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=5, ge=1, le=10)
    sort_by: Literal["relevance", "last_updated", "submitted"] = "relevance"
    sort_order: Literal["ascending", "descending"] = "descending"


class ComparePapersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] = Field(min_length=2, max_length=5)
    topic: str = Field(min_length=3, max_length=200)
    include_future_work: bool = True

    @field_validator("paper_ids")
    @classmethod
    def validate_comparison_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("paper_ids 不能重复。")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in value
        ):
            raise ValueError("paper_ids 只能包含正整数。")
        return value


class CitationGraphInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=100)
    min_citations: int = Field(default=0, ge=0, le=1000)
    paper_id: int | None = Field(default=None, gt=0)
    include_isolated: bool = True


class ResearchOutlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: int = Field(gt=0)
    topic: str = Field(default="", max_length=200)


class VerifiableReferencesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("paper_ids")
    @classmethod
    def validate_reference_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("paper_ids 不能重复。")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in value
        ):
            raise ValueError("paper_ids 只能包含正整数。")
        return value


class GetPaperStructureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: int = Field(gt=0)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_elements_per_page: int = Field(default=20, ge=1, le=100)
    include_text: bool = True


class SourceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal[
        "local_full_text",
        "external_arxiv_abstract",
    ]
    paper_id: int | None = Field(default=None, gt=0)
    page_number: int | None = Field(default=None, gt=0)
    chunk_id: int | None = Field(default=None, gt=0)
    arxiv_id: str | None = Field(default=None, max_length=80)
    title: str | None = Field(default=None, max_length=1024)
    content_hash: str | None = Field(default=None, max_length=64)
    excerpt: str = Field(min_length=1, max_length=10000)
    source_url: str | None = Field(default=None, max_length=1024)


class ToolErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "validation_error",
        "not_found",
        "empty_result",
        "timeout",
        "rate_limited",
        "external_service_error",
        "internal_error",
    ]
    code: str
    message: str
    retryable: bool = False


class StandardToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    tool_name: str
    status: Literal["success", "empty_result", "error"]
    data: dict[str, Any]
    summary: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    error: ToolErrorInfo | None = None


class PaperStructureElement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element_id: int
    element_index: int
    element_type: str
    page_number: int
    section_path: str | None = None
    extraction_status: str
    bbox: list[float] | None = None
    chunk_ids: list[int] = Field(default_factory=list)
    text: str | None = None
    text_truncated: bool = False


class PaperStructurePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int
    parse_status: str
    char_count: int
    element_count: int
    chunk_ids: list[int] = Field(default_factory=list)
    elements: list[PaperStructureElement] = Field(default_factory=list)


class PaperStructureSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str
    page_numbers: list[int] = Field(default_factory=list)
    element_ids: list[int] = Field(default_factory=list)
    chunk_ids: list[int] = Field(default_factory=list)


class GetPaperStructureOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: int
    title: str
    authors: str | None = None
    publication_year: int | None = None
    parse_status: str
    index_status: str
    page_count: int
    returned_page_count: int
    sections: list[PaperStructureSection]
    pages: list[PaperStructurePage]
    result_count: int
    source: Literal["local_structured_paper"]


class SearchLocalPapersOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    result_count: int = Field(ge=0)
    source: Literal["local_hybrid_retrieval"]
    sources: list[SourceCandidate]
    result_paper_ids: list[int]


class SearchArxivOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    result_count: int = Field(ge=0)
    source: Literal["arxiv_official_api"]
    total_results: int = Field(ge=0)
    sources: list[SourceCandidate]


class ComparePapersOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    selected_paper_ids: list[int]
    comparison: dict[str, Any]
    source: Literal["existing_literature_review_service"]
    sources: list[SourceCandidate]
    result_count: int = Field(ge=0)
    llm_diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class CitationGraphOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph: dict[str, Any]
    result_count: int = Field(ge=0)
    source: Literal["local_citation_graph"]
    sources: list[SourceCandidate] = Field(default_factory=list)


class KnowledgeGraphInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=300)
    paper_id: int | None = Field(default=None, gt=0)
    node_types: list[str] = Field(default_factory=list, max_length=8)
    relation_types: list[str] = Field(default_factory=list, max_length=12)
    limit: int = Field(default=100, ge=1, le=300)


class KnowledgeMethodComparisonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] = Field(min_length=2, max_length=5)

    @field_validator("paper_ids")
    @classmethod
    def validate_method_paper_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("paper_ids 不能重复。")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in value
        ):
            raise ValueError("paper_ids 只能包含正整数。")
        return value


class KnowledgePathInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_paper_id: int = Field(gt=0)
    target_paper_id: int = Field(gt=0)
    max_depth: int = Field(default=5, ge=1, le=8)


class KnowledgeGapInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(default="", max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class KnowledgeGraphOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: dict[str, Any]
    result_count: int = Field(ge=0)
    source: Literal["local_scientific_knowledge_graph"]
    sources: list[SourceCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ResearchOutlineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outline: dict[str, Any]
    result_count: int = Field(ge=0)
    source: Literal["existing_academic_outline_service"]
    sources: list[SourceCandidate]


class VerifiableReferencesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: dict[str, Any]
    result_count: int = Field(ge=0)
    source: Literal["existing_verifiable_references_service"]
    sources: list[SourceCandidate]


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str
    content: str
    citation_ids: list[str]


class ReportModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    executive_summary: str = Field(min_length=1, max_length=5000)
    sections: list[ReportSection] = Field(min_length=1, max_length=12)
    limitations: str = Field(default="", max_length=5000)


class ResearchAgentPlanEstimate(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_count: int
    tool_call_estimate: int
    arxiv_request_estimate: int
    llm_call_estimate: int


class ResearchAgentUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    nodes: int
    tool_calls: int
    llm_calls: int
    arxiv_requests: int
    elapsed_seconds: int | None


class ResearchAgentWorkerState(BaseModel):
    model_config = ConfigDict(extra="allow")

    lease_status: Literal["active", "expired", "released"]
    lease_generation: int
    lease_expires_at: str | None
    heartbeat_at: str | None
    resume_count: int


class ResearchAgentNodeState(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    node_key: str
    node_type: str
    tool_name: str | None
    input: dict[str, Any]
    depends_on: list[str]
    reason: str
    completion_condition: str
    output_summary: dict[str, Any] | None
    status: str
    retry_count: int
    retry_limit: int
    error: str | None
    started_at: str | None
    completed_at: str | None
    execution_input: dict[str, Any] | None
    structured_output: dict[str, Any] | None
    error_output: dict[str, Any] | None
    strategy_decision: dict[str, Any] | None


class ResearchAgentToolCallState(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    node_id: str
    tool_name: str
    attempt: int
    arguments_summary: dict[str, Any]
    result_summary: dict[str, Any] | None
    status: str
    duration_ms: int | None
    error: str | None
    started_at: str | None
    completed_at: str | None


class ResearchAgentEvidenceState(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    node_id: str
    tool_call_id: str
    source_type: str
    paper_id: int | None
    page_number: int | None
    chunk_id: int | None
    arxiv_id: str | None
    title: str | None
    content_hash: str | None
    excerpt: str
    source_url: str | None
    is_valid: bool
    validation_error: str | None


class ResearchAgentPlanHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int | None
    action: str | None
    change_reason: str | None
    plan: ValidatedPlan | None
    created_at: str | None


class ResearchAgentReplanDecision(BaseModel):
    model_config = ConfigDict(extra="allow")

    trigger: str
    old_plan_version: int
    new_plan_version: int | None
    reason: str
    planner_source: str
    added_nodes: list[str]
    accepted: bool
    error_category: str | None = None
    created_at: str | None


class ResearchAgentRecovery(BaseModel):
    model_config = ConfigDict(extra="allow")

    reason: str
    previous_status: str
    generation: int
    completed_nodes_reused: list[str]
    unfinished_nodes: list[str]
    created_at: str | None


class ResearchAgentRunData(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    goal: str
    status: str
    topic: str
    research_question: str
    time_range: str | None
    analysis_dimensions: list[str]
    plan: ValidatedPlan | None
    plan_estimate: ResearchAgentPlanEstimate | None
    plan_history: list[ResearchAgentPlanHistoryEntry]
    plan_version: int
    replan_count: int
    budget: dict[str, int]
    usage: ResearchAgentUsage
    cancel_requested: bool
    error: str | None
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    nodes: list[ResearchAgentNodeState]
    tool_calls: list[ResearchAgentToolCallState]
    evidence: list[ResearchAgentEvidenceState]
    report_available: bool
    llm_diagnostics: list[dict[str, Any]]
    worker_state: ResearchAgentWorkerState
    replan_decisions: list[ResearchAgentReplanDecision]
    recoveries: list[ResearchAgentRecovery]


class ResearchAgentRunEnvelope(BaseModel):
    success: Literal[True]
    data: ResearchAgentRunData
    error: None


class ResearchAgentReportPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    report: ReportModelOutput | None = None
    markdown: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    plan_version: int | None = None
    replan_count: int | None = None
    generation_mode: Literal["llm", "fallback"] | None = None


class ResearchAgentReportData(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    status: str
    valid_source_count: int
    report: ResearchAgentReportPayload
    created_at: str | None


class ResearchAgentReportEnvelope(BaseModel):
    success: Literal[True]
    data: ResearchAgentReportData
    error: None


class ResearchAgentToolCatalogItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    cost_class: Literal["local", "network", "llm"]
    max_attempts: int
    max_calls_per_run: int
    llm_calls_per_execution: int
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class ResearchAgentToolCatalogEnvelope(BaseModel):
    success: Literal[True]
    data: list[ResearchAgentToolCatalogItem]
    error: None


class ResearchAgentEnvelope(BaseModel):
    success: Literal[True]
    data: dict[str, Any]
    error: None


ResearchAgentPlanUpdate.model_rebuild()
