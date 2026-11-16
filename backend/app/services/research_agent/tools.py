from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.paper import Paper
from app.schemas.research_agent import (
    CitationGraphInput,
    CitationGraphOutput,
    ComparePapersInput,
    ComparePapersOutput,
    InspectLocalLibraryInput,
    InspectLocalLibraryOutput,
    ResearchOutlineInput,
    ResearchOutlineOutput,
    SearchArxivInput,
    SearchArxivOutput,
    SearchLocalPapersInput,
    SearchLocalPapersOutput,
    SourceCandidate,
    VerifiableReferencesInput,
    VerifiableReferencesOutput,
)
from app.services.academic_writing_service import (
    generate_academic_outline,
    list_verifiable_references,
)
from app.services.arxiv_service import search_arxiv
from app.services.citation_service import get_citation_graph
from app.services.literature_review_service import generate_literature_review
from app.services.paper_service import list_papers
from app.services.retrieval_service import search_chunks_hybrid


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class ToolContext:
    db: Session
    run_id: str
    node_id: str
    cancel_check: Callable[[], bool]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    side_effect: Literal["read_only"]
    timeout: float
    max_attempts: int
    max_calls_per_run: int
    cost_class: Literal["local", "network", "llm"]
    handler: Callable[[ToolContext, BaseModel], BaseModel]
    llm_calls_per_execution: int = 0


class ToolExecutionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.side_effect != "read_only":
            raise ValueError("Autonomous research agent tools must be read-only.")
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        if spec.max_attempts < 1 or spec.max_calls_per_run < 1:
            raise ValueError("Tool attempts and call limits must be positive.")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolExecutionError(
                f"工具 {name!r} 未在只读白名单中注册。",
                code="AGENT_TOOL_NOT_REGISTERED",
                retryable=False,
            )
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def validate_arguments(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> BaseModel:
        spec = self.get(name)
        try:
            return spec.input_schema.model_validate(arguments)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"工具 {name!r} 的参数未通过 Schema 校验。",
                code="AGENT_TOOL_INVALID_ARGUMENTS",
                retryable=False,
            ) from exc

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        spec = self.get(name)
        validated = self.validate_arguments(name, arguments)
        try:
            result = spec.handler(context, validated)
        except AppError as exc:
            raise ToolExecutionError(
                exc.message,
                code=exc.code,
                retryable=exc.code
                in {
                    "ARXIV_TIMEOUT",
                    "ARXIV_NETWORK_ERROR",
                    "ARXIV_RATE_LIMITED",
                    "ARXIV_UPSTREAM_ERROR",
                    "REVIEW_TIMEOUT",
                },
            ) from exc
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(
                (
                    f"工具 {name!r} 执行失败"
                    f"（{type(exc).__name__}）：{exc}"
                ),
                code="AGENT_TOOL_EXECUTION_FAILED",
                retryable=True,
            ) from exc
        try:
            output = spec.output_schema.model_validate(result)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"工具 {name!r} 返回结果未通过输出 Schema 校验。",
                code="AGENT_TOOL_INVALID_OUTPUT",
                retryable=False,
            ) from exc
        return output.model_dump(mode="json")


def _local_source(
    *,
    paper_id: int,
    page_number: int,
    chunk_id: int,
    title: str | None,
    excerpt: str,
    content_hash: str | None = None,
) -> SourceCandidate:
    return SourceCandidate(
        source_type="local_full_text",
        paper_id=paper_id,
        page_number=page_number,
        chunk_id=chunk_id,
        title=title,
        content_hash=content_hash,
        excerpt=excerpt,
    )


def _external_source(item: dict[str, Any]) -> SourceCandidate:
    return SourceCandidate(
        source_type="external_arxiv_abstract",
        arxiv_id=str(item["arxiv_id"]),
        title=str(item.get("title") or ""),
        excerpt=str(item.get("summary") or ""),
        source_url=item.get("abs_url"),
    )


def _inspect_local_library(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = InspectLocalLibraryInput.model_validate(validated)
    papers = list_papers(context.db)
    query = params.query.casefold().strip()
    if query:
        papers = [
            paper
            for paper in papers
            if query in paper.title.casefold()
            or query in (paper.authors or "").casefold()
        ]
    selected = papers[: params.limit]
    return InspectLocalLibraryOutput(
        paper_count=len(papers),
        result_count=len(selected),
        papers=[
            {
                "paper_id": paper.id,
                "title": paper.title,
                "authors": paper.authors,
                "publication_year": paper.publication_year,
                "parse_status": paper.parse_status,
                "index_status": paper.index_status,
            }
            for paper in selected
        ],
    )


def _search_local_papers(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = SearchLocalPapersInput.model_validate(validated)
    paper_ids = params.paper_ids
    if not paper_ids:
        paper_ids = [
            paper_id
            for (paper_id,) in (
                context.db.query(Paper.id)
                .order_by(Paper.id.asc())
                .limit(10)
                .all()
            )
        ]
    per_paper_limit = max(1, params.limit)
    grouped: list[list[dict[str, Any]]] = []
    for paper_id in paper_ids:
        if context.cancel_check():
            raise ToolExecutionError(
                "研究任务已取消。",
                code="AGENT_CANCELLED",
                retryable=False,
            )
        items = search_chunks_hybrid(
            db=context.db,
            query=params.query,
            limit=per_paper_limit,
            paper_id=paper_id,
        )
        grouped.append(
            [
                item
                for item in items
                if int(item.get("paper_id", -1)) == paper_id
            ]
        )

    results: list[dict[str, Any]] = []
    positions = [0] * len(grouped)
    while len(results) < params.limit:
        added = False
        for index, items in enumerate(grouped):
            if positions[index] >= len(items):
                continue
            results.append(items[positions[index]])
            positions[index] += 1
            added = True
            if len(results) >= params.limit:
                break
        if not added:
            break

    sources = [
        _local_source(
            paper_id=int(item["paper_id"]),
            page_number=int(item["page_number"]),
            chunk_id=int(item["chunk_id"]),
            title=item.get("paper_title"),
            excerpt=str(item.get("text") or ""),
        )
        for item in results
    ]
    result_paper_ids = list(
        dict.fromkeys(source.paper_id for source in sources if source.paper_id)
    )
    return SearchLocalPapersOutput(
        query=params.query,
        result_count=len(sources),
        source="local_hybrid_retrieval",
        sources=sources,
        result_paper_ids=result_paper_ids,
    )


def _search_external_papers(
    _context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = SearchArxivInput.model_validate(validated)
    payload = search_arxiv(
        query=params.query,
        field=params.field,
        page=params.page,
        page_size=params.page_size,
        sort_by=params.sort_by,
        sort_order=params.sort_order,
    )
    sources = [_external_source(item) for item in payload["results"]]
    return SearchArxivOutput(
        query=params.query,
        result_count=len(sources),
        source="arxiv_official_api",
        total_results=int(payload.get("total_results") or len(sources)),
        sources=sources,
    )


def _compare_papers(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = ComparePapersInput.model_validate(validated)
    payload = generate_literature_review(
        context.db,
        paper_ids=params.paper_ids,
        topic=params.topic,
        include_future_work=params.include_future_work,
    )
    sources: list[SourceCandidate] = []
    seen: set[tuple[int, int]] = set()
    for item in payload.get("sources", []):
        key = (int(item["paper_id"]), int(item["chunk_id"]))
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            _local_source(
                paper_id=key[0],
                page_number=int(item["page_number"]),
                chunk_id=key[1],
                title=item.get("title"),
                excerpt=str(item.get("evidence") or ""),
            )
        )
    return ComparePapersOutput(
        topic=params.topic,
        selected_paper_ids=params.paper_ids,
        comparison={
            "title": payload.get("title"),
            "sections": payload.get("sections", []),
            "dimension_comparison": payload.get("dimension_comparison", []),
            "paper_coverage": payload.get("paper_coverage", []),
        },
        source="existing_literature_review_service",
        sources=sources,
        result_count=len(sources),
    )


def _get_citation_graph(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = CitationGraphInput.model_validate(validated)
    payload = get_citation_graph(
        context.db,
        limit=params.limit,
        min_citations=params.min_citations,
        paper_id=params.paper_id,
        include_isolated=params.include_isolated,
    )
    return CitationGraphOutput(
        graph=payload,
        result_count=len(payload.get("nodes", [])),
        source="local_citation_graph",
    )


def _generate_outline(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = ResearchOutlineInput.model_validate(validated)
    payload = generate_academic_outline(
        context.db,
        paper_id=params.paper_id,
        topic=params.topic,
    )
    sources = [
        _local_source(
            paper_id=int(item["paper_id"]),
            page_number=int(item["page_number"]),
            chunk_id=int(item["chunk_id"]),
            title=item.get("title"),
            excerpt=str(item.get("excerpt") or ""),
        )
        for item in payload.get("evidence_sources", [])
    ]
    return ResearchOutlineOutput(
        outline=payload,
        result_count=len(sources),
        source="existing_academic_outline_service",
        sources=sources,
    )


def _get_references(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = VerifiableReferencesInput.model_validate(validated)
    payload = list_verifiable_references(
        context.db,
        paper_ids=params.paper_ids,
    )
    sources = [
        _local_source(
            paper_id=int(item["paper_id"]),
            page_number=int(item["evidence"]["page_number"]),
            chunk_id=int(item["evidence"]["chunk_id"]),
            title=item.get("title"),
            excerpt=str(item["evidence"].get("excerpt") or ""),
        )
        for item in payload.get("references", [])
        if item.get("verified_local") and item.get("evidence")
    ]
    return VerifiableReferencesOutput(
        references=payload,
        result_count=len(sources),
        source="existing_verifiable_references_service",
        sources=sources,
    )


TOOL_REGISTRY = ToolRegistry()


TOOL_REGISTRY.register(
    ToolSpec(
        name="inspect_local_library",
        description="读取本地论文库元数据，确认可检索论文及其解析/索引状态。",
        input_schema=InspectLocalLibraryInput,
        output_schema=InspectLocalLibraryOutput,
        side_effect="read_only",
        timeout=10.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_inspect_local_library,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="search_local_papers",
        description="调用现有 BM25/BGE-M3/RRF 混合检索，仅返回本地全文 Chunk。",
        input_schema=SearchLocalPapersInput,
        output_schema=SearchLocalPapersOutput,
        side_effect="read_only",
        timeout=60.0,
        max_attempts=2,
        max_calls_per_run=3,
        cost_class="local",
        handler=_search_local_papers,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="search_arxiv",
        description="调用现有 arXiv 官方 API 搜索服务，仅获得题录与摘要。",
        input_schema=SearchArxivInput,
        output_schema=SearchArxivOutput,
        side_effect="read_only",
        timeout=25.0,
        max_attempts=2,
        max_calls_per_run=3,
        cost_class="network",
        handler=_search_external_papers,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="compare_papers",
        description="调用现有多文证据综述服务比较 2 至 5 篇本地论文。",
        input_schema=ComparePapersInput,
        output_schema=ComparePapersOutput,
        side_effect="read_only",
        timeout=90.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="llm",
        handler=_compare_papers,
        llm_calls_per_execution=1,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="get_citation_graph",
        description="读取现有本地引用关系图和 PageRank 统计。",
        input_schema=CitationGraphInput,
        output_schema=CitationGraphOutput,
        side_effect="read_only",
        timeout=30.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_get_citation_graph,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="generate_research_outline",
        description="调用现有单篇论文证据大纲服务。",
        input_schema=ResearchOutlineInput,
        output_schema=ResearchOutlineOutput,
        side_effect="read_only",
        timeout=60.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_generate_outline,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="get_verifiable_references",
        description="调用现有可核验 References 服务，仅保留有本地 Chunk 的证据。",
        input_schema=VerifiableReferencesInput,
        output_schema=VerifiableReferencesOutput,
        side_effect="read_only",
        timeout=30.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_get_references,
    )
)
