from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_nodes: int = Field(default=8, ge=1, le=8)
    max_tool_calls: int = Field(default=12, ge=1, le=12)
    max_llm_calls: int = Field(default=4, ge=0, le=4)
    max_arxiv_requests: int = Field(default=3, ge=0, le=3)
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


class CitationGraphOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph: dict[str, Any]
    result_count: int = Field(ge=0)
    source: Literal["local_citation_graph"]
    sources: list[SourceCandidate] = Field(default_factory=list)


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


class ResearchAgentEnvelope(BaseModel):
    success: Literal[True]
    data: dict[str, Any]
    error: None
