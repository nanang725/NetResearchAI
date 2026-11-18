from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.schemas.research_agent import (
    CitationGraphInput,
    CitationGraphOutput,
    ComparePapersInput,
    ComparePapersOutput,
    GetPaperStructureInput,
    GetPaperStructureOutput,
    InspectLocalLibraryInput,
    InspectLocalLibraryOutput,
    KnowledgeGapInput,
    KnowledgeGraphInput,
    KnowledgeGraphOutput,
    KnowledgeMethodComparisonInput,
    KnowledgePathInput,
    ResearchOutlineInput,
    ResearchOutlineOutput,
    SearchArxivInput,
    SearchArxivOutput,
    SearchLocalPapersInput,
    SearchLocalPapersOutput,
    SourceCandidate,
    StandardToolOutput,
    VerifiableReferencesInput,
    VerifiableReferencesOutput,
)
from app.services.academic_writing_service import (
    generate_academic_outline,
    list_verifiable_references,
)
from app.services.arxiv_service import search_arxiv
from app.services.citation_service import get_citation_graph
from app.services.knowledge_graph_service import (
    compare_methods,
    explore_research_gaps,
    find_relation_path,
    get_knowledge_graph,
)
from app.services.literature_review_service import generate_literature_review
from app.services.paper_service import get_paper, list_papers
from app.services.retrieval_service import search_chunks_hybrid


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class ToolContext:
    db: Session
    run_id: str
    node_id: str
    cancel_check: Callable[[], bool]
    consume_llm_call: Callable[[], None] | None = None


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
    dynamic_llm_calls: bool = False


class ToolExecutionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        category: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.category = category or classify_tool_error(code)
        self.details = details or {}


def classify_tool_error(code: str) -> str:
    normalized = code.upper()
    if (
        normalized in {
            "AGENT_TOOL_INVALID_ARGUMENTS",
            "AGENT_BINDING_INVALID_PATH",
            "AGENT_BINDING_FIELD_NOT_FOUND",
            "AGENT_BINDING_TARGET_SCHEMA_INVALID",
        }
        or "VALIDATION" in normalized
    ):
        return "validation_error"
    if "NOT_FOUND" in normalized or normalized == "NOT_FOUND":
        return "not_found"
    if "TIMEOUT" in normalized:
        return "timeout"
    if "RATE_LIMIT" in normalized or "RATE_LIMITED" in normalized:
        return "rate_limited"
    if (
        normalized.startswith("REVIEW_")
        or normalized.startswith("LLM_")
        or "INVALID_RESPONSE" in normalized
    ):
        return "external_service_error"
    if any(
        marker in normalized
        for marker in (
            "NETWORK",
            "UPSTREAM",
            "EXTERNAL_SERVICE",
        )
    ):
        return "external_service_error"
    return "internal_error"


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
                category="validation_error",
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
                category=classify_tool_error(exc.code),
                details=exc.details,
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
                category="internal_error",
            ) from exc
        try:
            output = spec.output_schema.model_validate(result)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"工具 {name!r} 返回结果未通过输出 Schema 校验。",
                code="AGENT_TOOL_INVALID_OUTPUT",
                retryable=False,
                category="internal_error",
            ) from exc
        data = output.model_dump(mode="json")
        result_count = data.get("result_count")
        status = (
            "empty_result"
            if isinstance(result_count, int) and result_count == 0
            else "success"
        )
        standard = StandardToolOutput(
            success=True,
            tool_name=name,
            status=status,
            data=data,
            summary={
                "source": data.get("source"),
                "result_count": result_count if isinstance(result_count, int) else None,
            },
            warnings=[],
            error=None,
        )
        return {
            **data,
            **standard.model_dump(mode="json"),
        }


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
    if not params.paper_ids:
        results = search_chunks_hybrid(
            db=context.db,
            query=params.query,
            limit=params.limit,
            paper_id=None,
        )
    else:
        grouped: list[list[dict[str, Any]]] = []
        for paper_id in params.paper_ids:
            if context.cancel_check():
                raise ToolExecutionError(
                    "研究任务已取消。",
                    code="AGENT_CANCELLED",
                    retryable=False,
                )
            items = search_chunks_hybrid(
                db=context.db,
                query=params.query,
                limit=params.limit,
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
        consume_llm_call=context.consume_llm_call,
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
        llm_diagnostics=payload.get("llm_diagnostics", []),
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


def _knowledge_sources(
    edges: list[dict[str, Any]],
) -> list[SourceCandidate]:
    sources: list[SourceCandidate] = []
    seen: set[tuple[int, int]] = set()
    for edge in edges:
        paper_id = edge.get("paper_id")
        page_number = edge.get("page_number")
        chunk_id = edge.get("chunk_id")
        evidence = str(edge.get("evidence_text") or "").strip()
        if not paper_id or not page_number or not chunk_id or not evidence:
            continue
        key = (int(paper_id), int(chunk_id))
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            _local_source(
                paper_id=key[0],
                page_number=int(page_number),
                chunk_id=key[1],
                title=edge.get("paper_title"),
                excerpt=evidence,
            )
        )
    return sources


def _explore_knowledge_graph(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = KnowledgeGraphInput.model_validate(validated)
    payload = get_knowledge_graph(
        context.db,
        node_types=params.node_types or None,
        relation_types=params.relation_types or None,
        paper_id=params.paper_id,
        query=params.query,
        limit=params.limit,
    )
    sources = _knowledge_sources(payload.get("edges", []))
    return KnowledgeGraphOutput(
        result=payload,
        result_count=len(payload.get("nodes", [])),
        source="local_scientific_knowledge_graph",
        sources=sources,
        warnings=(
            []
            if sources
            else ["当前图谱筛选结果没有可定位的本地 Chunk 证据。"]
        ),
    )


def _compare_knowledge_methods(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = KnowledgeMethodComparisonInput.model_validate(validated)
    payload = compare_methods(context.db, paper_ids=params.paper_ids)
    edges = [
        evidence
        for method in payload.get("methods", [])
        for evidence in method.get("evidence", [])
    ]
    sources = _knowledge_sources(edges)
    return KnowledgeGraphOutput(
        result=payload,
        result_count=len(payload.get("methods", [])),
        source="local_scientific_knowledge_graph",
        sources=sources,
    )


def _find_knowledge_path(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = KnowledgePathInput.model_validate(validated)
    payload = find_relation_path(
        context.db,
        source_paper_id=params.source_paper_id,
        target_paper_id=params.target_paper_id,
        max_depth=params.max_depth,
    )
    sources = _knowledge_sources(payload.get("edges", []))
    return KnowledgeGraphOutput(
        result=payload,
        result_count=len(payload.get("nodes", [])),
        source="local_scientific_knowledge_graph",
        sources=sources,
    )


def _explore_knowledge_gaps(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = KnowledgeGapInput.model_validate(validated)
    payload = explore_research_gaps(
        context.db,
        topic=params.topic,
        limit=params.limit,
    )
    edges = [
        evidence
        for gap in payload.get("gaps", [])
        for evidence in gap.get("evidence", [])
    ]
    sources = _knowledge_sources(edges)
    return KnowledgeGraphOutput(
        result=payload,
        result_count=len(payload.get("gaps", [])),
        source="local_scientific_knowledge_graph",
        sources=sources,
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


def _parse_json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _get_paper_structure(
    context: ToolContext,
    validated: BaseModel,
) -> BaseModel:
    params = GetPaperStructureInput.model_validate(validated)
    paper = get_paper(context.db, params.paper_id, with_pages=True)
    chunks_by_element: dict[int, list[int]] = {}
    chunks_by_page: dict[int, list[int]] = {}
    for chunk in paper.chunks:
        chunks_by_page.setdefault(chunk.page_number, []).append(chunk.id)
        if chunk.element_id is not None:
            chunks_by_element.setdefault(chunk.element_id, []).append(chunk.id)

    pages = []
    sections: dict[str, dict[str, Any]] = {}
    returned_element_count = 0
    for page in sorted(paper.pages, key=lambda item: item.page_number)[
        : params.max_pages
    ]:
        elements = []
        for element in page.elements[: params.max_elements_per_page]:
            text = None
            truncated = False
            if params.include_text and element.text:
                text = element.text[:600]
                truncated = len(element.text) > len(text)
            bbox = _parse_json_value(element.bbox)
            bbox_values = (
                [float(item) for item in bbox]
                if isinstance(bbox, list)
                and len(bbox) == 4
                and all(isinstance(item, (int, float)) for item in bbox)
                else None
            )
            chunk_ids = sorted(chunks_by_element.get(element.id, []))
            element_payload = {
                "element_id": element.id,
                "element_index": element.element_index,
                "element_type": element.element_type,
                "page_number": element.page_number,
                "section_path": element.section_path,
                "extraction_status": element.extraction_status,
                "bbox": bbox_values,
                "chunk_ids": chunk_ids,
                "text": text,
                "text_truncated": truncated,
            }
            elements.append(element_payload)
            returned_element_count += 1
            if element.section_path:
                section = sections.setdefault(
                    element.section_path,
                    {
                        "heading": element.section_path,
                        "page_numbers": [],
                        "element_ids": [],
                        "chunk_ids": [],
                    },
                )
                if element.page_number not in section["page_numbers"]:
                    section["page_numbers"].append(element.page_number)
                section["element_ids"].append(element.id)
                section["chunk_ids"].extend(chunk_ids)
        pages.append(
            {
                "page_number": page.page_number,
                "parse_status": page.parse_status,
                "char_count": page.char_count,
                "element_count": len(page.elements),
                "chunk_ids": sorted(chunks_by_page.get(page.page_number, [])),
                "elements": elements,
            }
        )
    for section in sections.values():
        section["page_numbers"].sort()
        section["element_ids"] = list(dict.fromkeys(section["element_ids"]))
        section["chunk_ids"] = list(dict.fromkeys(section["chunk_ids"]))
    return GetPaperStructureOutput(
        paper_id=paper.id,
        title=paper.title,
        authors=paper.authors,
        publication_year=paper.publication_year,
        parse_status=paper.parse_status,
        index_status=paper.index_status,
        page_count=paper.page_count,
        returned_page_count=len(pages),
        sections=list(sections.values()),
        pages=pages,
        result_count=returned_element_count,
        source="local_structured_paper",
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
        name="explore_knowledge_graph",
        description=(
            "查询本地科研知识图谱，返回带论文、页码、Chunk 和证据文本的"
            "科研实体、跨论文关系与 PageRank 指标。"
        ),
        input_schema=KnowledgeGraphInput,
        output_schema=KnowledgeGraphOutput,
        side_effect="read_only",
        timeout=30.0,
        max_attempts=1,
        max_calls_per_run=2,
        cost_class="local",
        handler=_explore_knowledge_graph,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="compare_knowledge_methods",
        description="比较 2 至 5 篇本地论文中带证据的方法实体。",
        input_schema=KnowledgeMethodComparisonInput,
        output_schema=KnowledgeGraphOutput,
        side_effect="read_only",
        timeout=30.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_compare_knowledge_methods,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="find_knowledge_path",
        description="查找两篇本地论文之间的最短知识关系路径及逐边证据。",
        input_schema=KnowledgePathInput,
        output_schema=KnowledgeGraphOutput,
        side_effect="read_only",
        timeout=20.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_find_knowledge_path,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="explore_research_gaps",
        description="基于本地图谱覆盖和关系证据探索主题、方法与矛盾研究空白。",
        input_schema=KnowledgeGapInput,
        output_schema=KnowledgeGraphOutput,
        side_effect="read_only",
        timeout=20.0,
        max_attempts=1,
        max_calls_per_run=1,
        cost_class="local",
        handler=_explore_knowledge_gaps,
    )
)
TOOL_REGISTRY.register(
    ToolSpec(
        name="get_paper_structure",
        description=(
            "读取真实本地论文的页面、结构化元素、章节路径和关联 Chunk ID，"
            "不推断缺失结构。"
        ),
        input_schema=GetPaperStructureInput,
        output_schema=GetPaperStructureOutput,
        side_effect="read_only",
        timeout=30.0,
        max_attempts=1,
        max_calls_per_run=2,
        cost_class="local",
        handler=_get_paper_structure,
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
        llm_calls_per_execution=2,
        dynamic_llm_calls=True,
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
