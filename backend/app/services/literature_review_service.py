from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk
from app.services.llm_json import (
    LLMJSONError,
    finish_reason,
    parse_json_object,
    repair_messages,
    response_content,
    safe_diagnostics,
)
from app.services.llm_service import get_llm_client
from app.services.retrieval_service import search_chunks_hybrid


class ReviewSectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=8000)
    citation_ids: list[int] = Field(default_factory=list, max_length=20)


class ReviewComparisonItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: int
    summary: str = Field(min_length=1, max_length=1500)
    claim_kind: Literal["evidence", "inference"]
    citation_ids: list[int] = Field(default_factory=list, max_length=20)


class ReviewComparisonOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    methods: list[ReviewComparisonItemOutput] = Field(default_factory=list)
    results: list[ReviewComparisonItemOutput] = Field(default_factory=list)
    conclusions: list[ReviewComparisonItemOutput] = Field(default_factory=list)
    limitations: list[ReviewComparisonItemOutput] = Field(default_factory=list)


class ReviewFutureWorkOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["explicit", "inferred"]
    summary: str = Field(min_length=1, max_length=1000)
    citation_ids: list[int] = Field(min_length=1, max_length=20)


class LiteratureReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    sections: list[ReviewSectionOutput] = Field(min_length=1, max_length=20)
    comparison: ReviewComparisonOutput = Field(
        default_factory=ReviewComparisonOutput,
    )
    future_work: list[ReviewFutureWorkOutput] = Field(default_factory=list)


REVIEW_EVIDENCE_PER_PAPER = 4
REVIEW_DISCLAIMER = (
    "本综述由 AI 根据所选论文的检索证据辅助生成，不是论文原文，"
    "不构成新的论文证据；关键结论请回看引用片段。"
)
FUTURE_WORK_MARKERS = (
    "future work",
    "future direction",
    "future research",
    "limitation",
    "future",
    "未来工作",
    "未来方向",
    "未来研究",
    "研究局限",
    "局限",
    "展望",
    "后续工作",
)
COMPARISON_DIMENSIONS = (
    "methods",
    "results",
    "conclusions",
    "limitations",
)
COMPARISON_LABELS = {
    "methods": "研究方法",
    "results": "实验结果",
    "conclusions": "研究结论",
    "limitations": "局限性",
}
COMPARISON_MARKERS = {
    "methods": (
        "method",
        "approach",
        "algorithm",
        "framework",
        "architecture",
        "model",
        "方法",
        "算法",
        "模型",
        "架构",
        "框架",
        "设计",
    ),
    "results": (
        "result",
        "experiment",
        "evaluation",
        "performance",
        "accuracy",
        "latency",
        "throughput",
        "实验",
        "结果",
        "性能",
        "准确率",
        "评估",
        "指标",
    ),
    "conclusions": (
        "conclusion",
        "we conclude",
        "finding",
        "demonstrate",
        "indicate",
        "结论",
        "表明",
        "说明",
        "发现",
        "证明",
    ),
    "limitations": (
        "limitation",
        "future work",
        "future research",
        "局限",
        "不足",
        "未来工作",
        "未来研究",
    ),
}
INSUFFICIENT_EVIDENCE_MARKERS = (
    "证据不足",
    "当前证据不足",
    "无法确定",
    "无法确认",
    "未能确认",
)


def _paper_short(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    return normalized[:limit]


def _future_signal(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in FUTURE_WORK_MARKERS)


def _dimension_evidence_map(
    evidence: list[dict],
    papers: list[Paper],
) -> dict[str, dict[int, list[dict]]]:
    mapped: dict[str, dict[int, list[dict]]] = {
        dimension: {paper.id: [] for paper in papers}
        for dimension in COMPARISON_DIMENSIONS
    }
    for dimension in COMPARISON_DIMENSIONS:
        markers = COMPARISON_MARKERS[dimension]
        for paper in papers:
            for item in evidence:
                if item["paper_id"] != paper.id:
                    continue
                lowered = item["evidence"].casefold()
                if not any(marker in lowered for marker in markers):
                    continue
                if len(mapped[dimension][paper.id]) >= 2:
                    continue
                mapped[dimension][paper.id].append(item)
    return mapped


def _dimension_prompt_text(
    dimension_map: dict[str, dict[int, list[dict]]],
    papers: list[Paper],
) -> str:
    blocks = []
    for dimension in COMPARISON_DIMENSIONS:
        paper_blocks = []
        for paper in papers:
            items = dimension_map[dimension].get(paper.id, [])
            if items:
                ids = ", ".join(str(item["citation_id"]) for item in items)
                paper_blocks.append(
                    f"- paper_id={paper.id}; status=covered; "
                    f"allowed_citation_ids=[{ids}]"
                )
            else:
                paper_blocks.append(
                    f"- paper_id={paper.id}; status=insufficient_evidence; "
                    "allowed_citation_ids=[]"
                )
        blocks.append(
            f"{COMPARISON_LABELS[dimension]}（{dimension}）\n"
            + "\n".join(paper_blocks)
        )
    return "\n\n".join(blocks)


def _validated_citation_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise AppError(
            "综述返回了无效的证据引用。",
            code="REVIEW_INVALID_CITATION",
            status_code=502,
        )
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise AppError(
                "综述返回了不存在的证据编号。",
                code="REVIEW_INVALID_CITATION",
                status_code=502,
                details={"citation_id": item},
            )
        ids.append(item)
    return list(dict.fromkeys(ids))


def _future_chunks(
    db: Session,
    paper_ids: list[int],
    *,
    per_paper: int = 3,
) -> dict[int, list[PaperChunk]]:
    clauses = [
        PaperChunk.text.ilike(f"%{marker}%")
        for marker in FUTURE_WORK_MARKERS
    ]
    rows = (
        db.query(PaperChunk)
        .filter(
            PaperChunk.paper_id.in_(paper_ids),
            or_(*clauses),
        )
        .order_by(
            PaperChunk.paper_id.asc(),
            PaperChunk.page_number.asc(),
            PaperChunk.page_chunk_index.asc(),
        )
        .all()
    )
    grouped: dict[int, list[PaperChunk]] = defaultdict(list)
    for chunk in rows:
        if len(grouped[chunk.paper_id]) < per_paper:
            grouped[chunk.paper_id].append(chunk)
    return grouped


def collect_review_evidence(
    db: Session,
    paper_ids: list[int],
    topic: str,
    *,
    include_future_work: bool,
) -> tuple[list[Paper], list[dict], list[dict], list[dict]]:
    papers = (
        db.query(Paper)
        .filter(Paper.id.in_(paper_ids))
        .order_by(Paper.id.asc())
        .all()
    )
    paper_map = {paper.id: paper for paper in papers}
    missing = [paper_id for paper_id in paper_ids if paper_id not in paper_map]
    if missing:
        raise AppError(
            "综述包含不存在的论文。",
            code="PAPER_NOT_FOUND",
            status_code=404,
            details={"paper_ids": missing},
        )

    future_by_paper = (
        _future_chunks(db, paper_ids)
        if include_future_work
        else {}
    )
    evidence: list[dict] = []
    next_id = 1
    topic_evidence_counts: dict[int, int] = {}
    future_ids: set[int] = set()
    seen_chunk_ids: set[int] = set()
    for paper in papers:
        results = search_chunks_hybrid(
            db=db,
            query=topic,
            limit=REVIEW_EVIDENCE_PER_PAPER,
            paper_id=paper.id,
        )
        results = [
            item
            for item in results
            if int(item.get("paper_id", -1)) == paper.id
        ]
        topic_evidence_counts[paper.id] = len(results)
        for item in results:
            chunk_id = int(item["chunk_id"])
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            evidence.append(
                {
                    "citation_id": next_id,
                    "paper_id": paper.id,
                    "title": paper.title,
                    "page_number": int(item.get("page_number", 1)),
                    "chunk_id": chunk_id,
                    "evidence": _paper_short(str(item.get("text", "")), 1600),
                    "future_candidate": False,
                }
            )
            next_id += 1
        if include_future_work:
            for chunk in future_by_paper.get(paper.id, []):
                if chunk.id in seen_chunk_ids:
                    future_ids.add(
                        next(
                            item["citation_id"]
                            for item in evidence
                            if item["chunk_id"] == chunk.id
                        )
                    )
                    continue
                seen_chunk_ids.add(chunk.id)
                evidence.append(
                    {
                        "citation_id": next_id,
                        "paper_id": paper.id,
                        "title": paper.title,
                        "page_number": chunk.page_number,
                        "chunk_id": chunk.id,
                        "evidence": _paper_short(chunk.text, 1600),
                        "future_candidate": True,
                    }
                )
                future_ids.add(next_id)
                next_id += 1

    coverage = [
        {
            "paper_id": paper.id,
            "title": paper.title,
            "evidence_count": topic_evidence_counts.get(paper.id, 0),
            "status": (
                "covered"
                if topic_evidence_counts.get(paper.id, 0) > 0
                else "insufficient_evidence"
            ),
        }
        for paper in papers
    ]
    return papers, evidence, coverage, [
        item for item in evidence if item["citation_id"] in future_ids
    ]


def _build_review_prompt(
    topic: str,
    papers: list[Paper],
    evidence: list[dict],
    coverage: list[dict],
    dimension_map: dict[str, dict[int, list[dict]]],
    future_evidence: list[dict],
    include_future_work: bool,
) -> str:
    evidence_text = "\n\n".join(
        (
            f"【证据 {item['citation_id']}】\n"
            f"论文 ID：{item['paper_id']}\n"
            f"论文标题：{item['title']}\n"
            f"页码：{item['page_number']}\n"
            f"Chunk ID：{item['chunk_id']}\n"
            f"原文：{item['evidence']}"
        )
        for item in evidence
    )
    coverage_text = "\n".join(
        (
            f"- paper_id={item['paper_id']}，"
            f"title={item['title']}，"
            f"topic_evidence={item['evidence_count']}，"
            f"status={item['status']}"
        )
        for item in coverage
    )
    dimension_text = _dimension_prompt_text(dimension_map, papers)
    paper_text = "\n".join(
        (
            f"- paper_id={paper.id}；title={paper.title}；"
            f"year={paper.publication_year or '未知'}；"
            f"authors={paper.authors or '未知'}"
        )
        for paper in papers
    )
    future_text = "\n".join(
        f"- citation_id={item['citation_id']}；{item['evidence']}"
        for item in future_evidence
    ) or "未检索到明确的 Future Work 候选片段。"
    future_instruction = (
        """
还必须输出 future_work 数组。每项必须引用实际证据 citation_ids：
- kind="explicit"：作者原文明确提出未来工作或研究方向。
- kind="inferred"：仅根据原文局限推断的潜在方向，必须明确标注为推断。
不得把推断标成 explicit，不得编造原文没有的方向。
"""
        if include_future_work
        else "用户未要求提取 Future Work，future_work 必须返回空数组。"
    )
    return f"""
你是严谨的科研综述助手。请仅依据下面提供的论文原文证据生成多文献综述。

【综述主题】
{topic}

【选定论文】
{paper_text}

【证据覆盖】
{coverage_text}

【四维比较可用证据】
{dimension_text}

【可用原文证据】
{evidence_text}

【Future Work 候选证据】
{future_text}

{future_instruction}

【强制规则】
1. 不得使用证据之外的知识补充论文事实。
2. 必须综合分析，不可简单逐篇拼接。
3. 相同点、差异、方法比较和局限必须引用 citation_ids。
4. 不允许生成不存在的论文标题、作者、页码、Chunk 或实验结果。
5. 证据不足的论文必须明确说明，不能悄悄忽略。
6. 只能使用给定证据编号，禁止自行编造来源。
7. 若证据不足以支持某项结论，明确写明“当前证据不足”。
8. comparison 中每个条目只能引用该 paper_id 名下、且属于当前比较维度
   allowed_citation_ids 的证据。没有对应证据时必须输出证据不足条目，
   citation_ids 使用空数组。
9. claim_kind="evidence" 只能用于证据直接支持的内容；
   跨证据归纳、推测或研究建议必须标为 claim_kind="inference"。

只返回合法 JSON，不要代码围栏，结构如下：
{{
  "title": "综述标题",
  "sections": [
    {{
      "heading": "章节标题",
      "content": "综合分析内容",
      "citation_ids": [1, 2]
    }}
  ],
  "comparison": {{
    "methods": [
      {{
        "paper_id": 1,
        "summary": "该论文的方法概述",
        "claim_kind": "evidence",
        "citation_ids": [1]
      }}
    ],
    "results": [],
    "conclusions": [],
    "limitations": []
  }},
  "future_work": [
    {{
      "kind": "explicit",
      "summary": "中文归纳",
      "citation_ids": [3]
    }}
  ]
}}
""".strip()


# Retained for v0.4 module-surface compatibility. Runtime generation always
# uses `_call_review_model_bounded` so every model call is budget-aware.
def _legacy_call_review_model(prompt: str) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    try:
        client = get_llm_client()
        response = client.chat.completions.create(
            model=settings.llm_model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是严格的学术综述生成器。只依据给定证据，"
                        "只输出合法 JSON。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=6000,
            timeout=75,
        )
    except AppError:
        raise
    except Exception as exc:
        exception_name = type(exc).__name__.casefold()
        raise AppError(
            (
                "综述生成超时，请稍后重试。"
                if "timeout" in exception_name
                else "综述生成服务暂时不可用。"
            ),
            code=(
                "REVIEW_TIMEOUT"
                if "timeout" in exception_name
                else "REVIEW_LLM_ERROR"
            ),
            status_code=504 if "timeout" in exception_name else 502,
            details={"provider_error": type(exc).__name__},
        ) from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise AppError(
            "综述服务返回了无法识别的结果。",
            code="REVIEW_INVALID_RESPONSE",
            status_code=502,
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise AppError(
            "综述服务未返回内容。",
            code="REVIEW_EMPTY_RESULT",
            status_code=502,
        )
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            "综述服务返回了无效 JSON。",
            code="REVIEW_INVALID_RESPONSE",
            status_code=502,
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            "综述服务返回了无效结构。",
            code="REVIEW_INVALID_RESPONSE",
            status_code=502,
        )
    return parsed, settings.llm_model_name


def _call_review_model_bounded(
    prompt: str,
    consume_llm_call: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    try:
        client = get_llm_client()
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            "Review LLM client could not be created.",
            code="REVIEW_LLM_ERROR",
            status_code=502,
            details={"provider_error": type(exc).__name__},
        ) from exc

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are a strict evidence-grounded literature review "
                "generator. Return one valid JSON object only. Keep the "
                "output compact: at most 3 sections, at most one comparison "
                "item per paper and dimension, and at most 2 future_work "
                "items. Do not add unsupported detail."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    schema_text = json.dumps(
        LiteratureReviewOutput.model_json_schema(),
        ensure_ascii=False,
    )
    diagnostics: list[dict[str, Any]] = []

    for attempt in (1, 2):
        if consume_llm_call is not None:
            consume_llm_call()
        try:
            response = client.chat.completions.create(
                model=settings.llm_model_name,
                messages=messages,
                temperature=0.1,
                max_tokens=6000 if attempt == 1 else 3000,
                timeout=55 if attempt == 1 else 25,
                response_format={"type": "json_object"},
            )
        except AppError:
            raise
        except Exception as exc:
            exception_name = type(exc).__name__.casefold()
            raise AppError(
                "Review generation timed out."
                if "timeout" in exception_name
                else "Review generation service is unavailable.",
                code=(
                    "REVIEW_TIMEOUT"
                    if "timeout" in exception_name
                    else "REVIEW_LLM_ERROR"
                ),
                status_code=504 if "timeout" in exception_name else 502,
                details={
                    "provider_error": type(exc).__name__,
                    "attempt": attempt,
                },
            ) from exc

        content: str | None = None
        try:
            content = response_content(response)
            parsed = parse_json_object(content)
            validated = LiteratureReviewOutput.model_validate(parsed)
        except (LLMJSONError, ValidationError) as exc:
            response_finish_reason = finish_reason(response)
            failure_kind = (
                "output_truncated"
                if response_finish_reason == "length"
                else (
                    exc.kind
                    if isinstance(exc, LLMJSONError)
                    else "schema_validation_error"
                )
            )
            diagnostics.append(
                safe_diagnostics(
                    stage="literature_review",
                    attempt=attempt,
                    content=content,
                    finish_reason=response_finish_reason,
                    failure_kind=failure_kind,
                    error=exc,
                )
            )
            if attempt == 1:
                if response_finish_reason == "length":
                    messages = [
                        {
                            "role": "system",
                            "content": (
                                "Return one compact, complete JSON object. "
                                "Do not add facts or sources. No Markdown."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"{prompt}\n\n"
                                "The previous response was truncated by the "
                                "output limit. Return a compact complete "
                                "object with at most 3 sections, at most one "
                                "comparison item per paper per dimension, "
                                "short section content, and at most 2 "
                                "future_work items. Keep only citations that "
                                "are valid for the relevant evidence."
                            ),
                        },
                    ]
                else:
                    messages = repair_messages(
                        invalid_content=content or "",
                        schema_text=schema_text,
                        instruction=(
                            "Keep the existing sections and comparison "
                            "items. Every item must keep evidence-based "
                            "claim kinds and only source identifiers "
                            "supplied by the original response."
                        ),
                    )
                continue
            raise AppError(
                (
                    "Review LLM output was truncated after one compact "
                    "repair attempt."
                    if failure_kind == "output_truncated"
                    else
                    "Review LLM JSON parsing failed after one repair attempt."
                    if failure_kind == "json_parse_error"
                    else "Review LLM output failed Schema validation after "
                    "one repair attempt."
                ),
                code=(
                    "REVIEW_LLM_OUTPUT_TRUNCATED"
                    if failure_kind == "output_truncated"
                    else (
                        "REVIEW_LLM_SCHEMA_ERROR"
                        if failure_kind == "schema_validation_error"
                        else (
                            "REVIEW_LLM_JSON_ERROR"
                            if failure_kind
                            in {"json_parse_error", "json_shape_error"}
                            else "REVIEW_LLM_RESPONSE_ERROR"
                        )
                    )
                ),
                status_code=502,
                details={
                    "attempts": attempt,
                    "diagnostics": diagnostics,
                },
            ) from exc
        payload = validated.model_dump(mode="json")
        if diagnostics:
            payload["_llm_diagnostics"] = diagnostics
        return payload, settings.llm_model_name

    raise AppError(
        "Review LLM did not return valid structured output.",
        code="REVIEW_LLM_SCHEMA_ERROR",
        status_code=502,
        details={"diagnostics": diagnostics},
    )


def generate_literature_review(
    db: Session,
    *,
    paper_ids: list[int],
    topic: str,
    include_future_work: bool,
    consume_llm_call: Callable[[], None] | None = None,
) -> dict:
    if len(paper_ids) < 2 or len(paper_ids) > 5:
        raise AppError(
            "综述必须选择 2 至 5 篇论文。",
            code="INVALID_PAPER_SELECTION",
            status_code=422,
        )
    if len(set(paper_ids)) != len(paper_ids):
        raise AppError(
            "综述论文不能重复选择。",
            code="DUPLICATE_PAPER_ID",
            status_code=422,
        )
    topic = topic.strip()
    if len(topic) < 3 or len(topic) > 200:
        raise AppError(
            "综述主题长度必须为 3 至 200 字符。",
            code="INVALID_REVIEW_TOPIC",
            status_code=422,
        )

    papers, evidence, coverage, future_evidence = collect_review_evidence(
        db,
        paper_ids,
        topic,
        include_future_work=include_future_work,
    )
    if not evidence:
        raise AppError(
            "所选论文没有可用于生成综述的相关证据。",
            code="REVIEW_INSUFFICIENT_EVIDENCE",
            status_code=422,
        )

    dimension_map = _dimension_evidence_map(evidence, papers)
    parsed, model_name = _call_review_model_bounded(
        _build_review_prompt(
            topic,
            papers,
            evidence,
            coverage,
            dimension_map,
            future_evidence,
            include_future_work,
        ),
        consume_llm_call=consume_llm_call,
    )
    evidence_by_id = {
        int(item["citation_id"]): item
        for item in evidence
    }
    def citation_payload(citation_id: int) -> dict:
        item = evidence_by_id.get(citation_id)
        if item is None:
            raise AppError(
                "综述返回了不存在的证据编号。",
                code="REVIEW_INVALID_CITATION",
                status_code=502,
                details={"citation_id": citation_id},
            )
        return {
            "paper_id": item["paper_id"],
            "title": item["title"],
            "page_number": item["page_number"],
            "chunk_id": item["chunk_id"],
            "evidence": item["evidence"],
        }

    sections = []
    for section in parsed.get("sections", []):
        if not isinstance(section, dict):
            continue
        heading = _paper_short(str(section.get("heading", "")), 160)
        content = _paper_short(str(section.get("content", "")), 8000)
        ids = section.get("citation_ids", [])
        if not heading or not content or not isinstance(ids, list):
            continue
        citation_ids = _validated_citation_ids(ids)
        if not citation_ids:
            continue
        citations = [citation_payload(citation_id) for citation_id in citation_ids]
        sections.append(
            {
                "heading": heading,
                "content": content,
                "citations": citations,
            }
        )
    if not sections:
        raise AppError(
            "综述没有生成可追溯的有效章节。",
            code="REVIEW_INVALID_CITATION",
            status_code=502,
        )

    comparison_raw = parsed.get("comparison", {})
    if comparison_raw is None:
        comparison_raw = {}
    if not isinstance(comparison_raw, dict):
        raise AppError(
            "综述返回了无效的四维比较结构。",
            code="REVIEW_INVALID_RESPONSE",
            status_code=502,
        )
    paper_map = {paper.id: paper for paper in papers}
    dimension_comparison = []
    for dimension in COMPARISON_DIMENSIONS:
        raw_items = comparison_raw.get(dimension, [])
        if not isinstance(raw_items, list):
            raw_items = []
        rows_by_paper: dict[int, dict] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            paper_id = item.get("paper_id")
            if (
                isinstance(paper_id, bool)
                or not isinstance(paper_id, int)
                or paper_id not in paper_map
            ):
                raise AppError(
                    "综述比较结果引用了未选择的论文。",
                    code="REVIEW_INVALID_PAPER_ID",
                    status_code=502,
                    details={"paper_id": paper_id, "dimension": dimension},
                )
            if paper_id in rows_by_paper:
                continue
            summary = _paper_short(str(item.get("summary", "")), 1500)
            if not summary:
                continue
            claim_kind = item.get("claim_kind")
            if claim_kind not in {"evidence", "inference"}:
                raise AppError(
                    "综述比较结果缺少有效的主张类型。",
                    code="REVIEW_INVALID_CLAIM_KIND",
                    status_code=502,
                    details={"dimension": dimension, "paper_id": paper_id},
                )
            ids = item.get("citation_ids", [])
            if not isinstance(ids, list):
                raise AppError(
                    "综述比较结果包含无效的引文列表。",
                    code="REVIEW_INVALID_CITATION",
                    status_code=502,
                )
            citation_ids = _validated_citation_ids(ids)
            allowed_ids = {
                int(candidate["citation_id"])
                for candidate in dimension_map[dimension].get(paper_id, [])
            }
            if citation_ids:
                outside = [
                    citation_id
                    for citation_id in citation_ids
                    if citation_id not in allowed_ids
                ]
                if outside:
                    raise AppError(
                        "综述比较引用不属于对应论文或比较维度。",
                        code="REVIEW_EVIDENCE_ATTRIBUTION_ERROR",
                        status_code=502,
                        details={
                            "dimension": dimension,
                            "paper_id": paper_id,
                            "citation_ids": outside,
                        },
                    )
                citations = [
                    citation_payload(citation_id)
                    for citation_id in citation_ids
                ]
                status = "covered"
            else:
                if not any(
                    marker in summary
                    for marker in INSUFFICIENT_EVIDENCE_MARKERS
                ):
                    raise AppError(
                        "重要比较论述缺少真实证据引用。",
                        code="REVIEW_INVALID_CITATION",
                        status_code=502,
                        details={
                            "dimension": dimension,
                            "paper_id": paper_id,
                        },
                    )
                citations = []
                claim_kind = "insufficient_evidence"
                status = "insufficient_evidence"
            rows_by_paper[paper_id] = {
                "paper_id": paper_id,
                "title": paper_map[paper_id].title,
                "summary": summary,
                "claim_kind": claim_kind,
                "evidence_status": status,
                "citations": citations,
            }
        for paper in papers:
            if paper.id not in rows_by_paper:
                rows_by_paper[paper.id] = {
                    "paper_id": paper.id,
                    "title": paper.title,
                    "summary": "当前检索证据不足以确认该维度的内容。",
                    "claim_kind": "insufficient_evidence",
                    "evidence_status": "insufficient_evidence",
                    "citations": [],
                }
        dimension_comparison.append(
            {
                "dimension": dimension,
                "label": COMPARISON_LABELS[dimension],
                "items": [
                    rows_by_paper[paper.id]
                    for paper in papers
                ],
            }
        )

    future_work = []
    if include_future_work:
        for item in parsed.get("future_work", []):
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind not in {"explicit", "inferred"}:
                continue
            ids = item.get("citation_ids", [])
            if not isinstance(ids, list) or not ids:
                continue
            citation_ids = _validated_citation_ids(ids)
            summary = _paper_short(str(item.get("summary", "")), 1000)
            if not summary:
                continue
            source = evidence_by_id[citation_ids[0]]
            if not _future_signal(source["evidence"]):
                continue
            future_work.append(
                {
                    "paper_id": source["paper_id"],
                    "title": source["title"],
                    "page_number": source["page_number"],
                    "chunk_id": source["chunk_id"],
                    "kind": kind,
                    "original_evidence": source["evidence"],
                    "summary": summary,
                }
            )

    source_map: dict[int, dict] = {}
    for section in sections:
        for citation in section["citations"]:
            source_map[citation["chunk_id"]] = citation
    for dimension in dimension_comparison:
        for item in dimension["items"]:
            for citation in item["citations"]:
                source_map[citation["chunk_id"]] = citation
    for item in future_work:
        source_map[item["chunk_id"]] = {
            "paper_id": item["paper_id"],
            "title": item["title"],
            "page_number": item["page_number"],
            "chunk_id": item["chunk_id"],
            "evidence": item["original_evidence"],
        }

    return {
        "title": _paper_short(str(parsed.get("title") or f"{topic}多文献综述"), 300),
        "topic": topic,
        "selected_paper_ids": paper_ids,
        "sections": sections,
        "paper_coverage": coverage,
        "dimension_comparison": dimension_comparison,
        "future_work": future_work,
        "sources": list(source_map.values()),
        "model": model_name,
        "llm_diagnostics": parsed.get("_llm_diagnostics", []),
        "disclaimer": REVIEW_DISCLAIMER,
    }
