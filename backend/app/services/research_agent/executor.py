from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk
from app.models.research_agent import (
    ResearchAgentArtifact,
    ResearchAgentEvidence,
    ResearchAgentNode,
    ResearchAgentRun,
    ResearchAgentToolCall,
)
from app.schemas.research_agent import (
    AgentBudgetRequest,
    ReportModelOutput,
    SourceCandidate,
    ValidatedPlan,
)
from app.services.arxiv_service import normalize_arxiv_id
from app.services.llm_service import get_llm_client
from app.services.research_agent.planner import (
    PlanValidationError,
    ResearchPlanner,
)
from app.services.research_agent.tools import (
    TOOL_REGISTRY,
    ToolContext,
    ToolExecutionError,
    ToolRegistry,
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "source": result.get("source"),
        "result_count": result.get("result_count", 0),
    }
    for key in (
        "paper_count",
        "total_results",
        "result_paper_ids",
        "query",
    ):
        if key in result:
            summary[key] = result[key]
    if "comparison" in result:
        comparison = result["comparison"]
        summary["comparison_section_count"] = len(
            comparison.get("sections", [])
        )
    if "graph" in result:
        graph = result["graph"]
        summary["graph_stats"] = graph.get("stats", {})
    if "references" in result:
        summary["verified_reference_count"] = result["references"].get(
            "verified_count",
            0,
        )
    if "outline" in result:
        summary["outline_section_count"] = len(
            result["outline"].get("sections", [])
        )
    return summary


def _evidence_error(
    *,
    source_type: str,
    paper_id: int | None,
    page_number: int | None,
    chunk_id: int | None,
    arxiv_id: str | None,
    content_hash: str,
    excerpt: str,
    source_url: str | None,
    message: str,
) -> ResearchAgentEvidence:
    return ResearchAgentEvidence(
        run_id="",
        node_id="",
        tool_call_id="",
        source_type=source_type,
        paper_id=paper_id,
        page_number=page_number,
        chunk_id=chunk_id,
        arxiv_id=arxiv_id,
        title=None,
        content_hash=content_hash,
        excerpt=excerpt[:4000],
        source_url=source_url,
        is_valid=False,
        validation_error=message,
    )


def validate_evidence(
    db: Session,
    source: SourceCandidate,
) -> tuple[ResearchAgentEvidence, bool]:
    """Validate identifiers against SQLite or external-abstract invariants."""
    if source.source_type == "local_full_text":
        if not all((source.paper_id, source.page_number, source.chunk_id)):
            return (
                _evidence_error(
                    source_type=source.source_type,
                    paper_id=source.paper_id,
                    page_number=source.page_number,
                    chunk_id=source.chunk_id,
                    arxiv_id=source.arxiv_id,
                    content_hash=source.content_hash or _content_hash(source.excerpt),
                    excerpt=source.excerpt,
                    source_url=source.source_url,
                    message="本地全文证据必须同时具有 paper_id、page_number 和 chunk_id。",
                ),
                False,
            )
        row = (
            db.query(PaperChunk, Paper)
            .join(Paper, Paper.id == PaperChunk.paper_id)
            .filter(
                PaperChunk.id == source.chunk_id,
                PaperChunk.paper_id == source.paper_id,
                PaperChunk.page_number == source.page_number,
            )
            .first()
        )
        if row is None:
            return (
                _evidence_error(
                    source_type=source.source_type,
                    paper_id=source.paper_id,
                    page_number=source.page_number,
                    chunk_id=source.chunk_id,
                    arxiv_id=None,
                    content_hash=source.content_hash or _content_hash(source.excerpt),
                    excerpt=source.excerpt,
                    source_url=None,
                    message="paper_id、page_number 或 chunk_id 与真实 SQLite 记录不一致。",
                ),
                False,
            )
        chunk, paper = row
        actual_hash = _content_hash(chunk.text)
        if source.content_hash and source.content_hash != actual_hash:
            return (
                _evidence_error(
                    source_type=source.source_type,
                    paper_id=source.paper_id,
                    page_number=source.page_number,
                    chunk_id=source.chunk_id,
                    arxiv_id=None,
                    content_hash=source.content_hash,
                    excerpt=source.excerpt,
                    source_url=None,
                    message="证据内容哈希与真实 Chunk 不一致。",
                ),
                False,
            )
        if not chunk.text.strip():
            return (
                _evidence_error(
                    source_type=source.source_type,
                    paper_id=source.paper_id,
                    page_number=source.page_number,
                    chunk_id=source.chunk_id,
                    arxiv_id=None,
                    content_hash=actual_hash,
                    excerpt=source.excerpt,
                    source_url=None,
                    message="真实 Chunk 没有可引用正文。",
                ),
                False,
            )
        return (
            ResearchAgentEvidence(
                run_id="",
                node_id="",
                tool_call_id="",
                source_type=source.source_type,
                paper_id=paper.id,
                page_number=chunk.page_number,
                chunk_id=chunk.id,
                arxiv_id=None,
                title=paper.title,
                content_hash=actual_hash,
                excerpt=chunk.text[:4000],
                source_url=None,
                is_valid=True,
            ),
            True,
        )

    if source.source_type == "external_arxiv_abstract":
        normalized_id = normalize_arxiv_id(source.arxiv_id)
        if (
            not normalized_id
            or source.paper_id is not None
            or source.page_number is not None
            or source.chunk_id is not None
        ):
            return (
                _evidence_error(
                    source_type=source.source_type,
                    paper_id=source.paper_id,
                    page_number=source.page_number,
                    chunk_id=source.chunk_id,
                    arxiv_id=source.arxiv_id,
                    content_hash=source.content_hash or _content_hash(source.excerpt),
                    excerpt=source.excerpt,
                    source_url=source.source_url,
                    message=(
                        "外部 arXiv 摘要必须有合法 arXiv ID，且不得伪造论文 ID、"
                        "页码或 Chunk ID。"
                    ),
                ),
                False,
            )
        return (
            ResearchAgentEvidence(
                run_id="",
                node_id="",
                tool_call_id="",
                source_type=source.source_type,
                paper_id=None,
                page_number=None,
                chunk_id=None,
                arxiv_id=normalized_id,
                title=source.title,
                content_hash=_content_hash(source.excerpt),
                excerpt=source.excerpt[:4000],
                source_url=source.source_url,
                is_valid=True,
            ),
            True,
        )

    return (
        _evidence_error(
            source_type=source.source_type,
            paper_id=source.paper_id,
            page_number=source.page_number,
            chunk_id=source.chunk_id,
            arxiv_id=source.arxiv_id,
            content_hash=source.content_hash or _content_hash(source.excerpt),
            excerpt=source.excerpt,
            source_url=source.source_url,
            message="不支持的证据来源类型。",
        ),
        False,
    )


def _store_evidence(
    db: Session,
    *,
    run_id: str,
    node_id: str,
    tool_call_id: str,
    source: SourceCandidate,
) -> bool:
    candidate, is_valid = validate_evidence(db, source)
    existing = (
        db.query(ResearchAgentEvidence)
        .filter(
            ResearchAgentEvidence.run_id == run_id,
            ResearchAgentEvidence.source_type == candidate.source_type,
            ResearchAgentEvidence.paper_id == candidate.paper_id,
            ResearchAgentEvidence.page_number == candidate.page_number,
            ResearchAgentEvidence.chunk_id == candidate.chunk_id,
            ResearchAgentEvidence.arxiv_id == candidate.arxiv_id,
            ResearchAgentEvidence.content_hash == candidate.content_hash,
        )
        .first()
    )
    if existing is not None:
        return existing.is_valid
    candidate.run_id = run_id
    candidate.node_id = node_id
    candidate.tool_call_id = tool_call_id
    db.add(candidate)
    return is_valid


def _budget_from_run(run: ResearchAgentRun) -> AgentBudgetRequest:
    return AgentBudgetRequest(
        max_nodes=run.max_nodes,
        max_tool_calls=run.max_tool_calls,
        max_llm_calls=run.max_llm_calls,
        max_arxiv_requests=run.max_arxiv_requests,
        max_execution_seconds=run.max_execution_seconds,
    )


def _plan_payload(plan: ValidatedPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def _persist_plan_nodes(
    db: Session,
    run: ResearchAgentRun,
    plan: ValidatedPlan,
    *,
    new_node_ids: set[str] | None = None,
) -> None:
    existing_keys = {
        row.node_key
        for row in (
            db.query(ResearchAgentNode.node_key)
            .filter(ResearchAgentNode.run_id == run.id)
            .all()
        )
    }
    for node in plan.nodes:
        if new_node_ids is not None and node.id not in new_node_ids:
            continue
        if node.id in existing_keys:
            continue
        db.add(
            ResearchAgentNode(
                run_id=run.id,
                node_key=node.id,
                node_type="tool",
                dependencies_json=_json_dump(node.depends_on),
                tool_name=node.tool_name,
                input_json=_json_dump(node.arguments),
                reason=node.reason,
                completion_condition=node.completion_condition,
                retry_limit=node.retry_limit,
                status="pending",
            )
        )
    run.plan_json = _json_dump(_plan_payload(plan))
    run.plan_version = plan.version


def _is_cancel_requested(
    run_id: str,
    session_factory: Callable[[], Session],
) -> bool:
    db = session_factory()
    try:
        row = (
            db.query(
                ResearchAgentRun.cancel_requested,
                ResearchAgentRun.status,
            )
            .filter(ResearchAgentRun.id == run_id)
            .first()
        )
        return bool(
            row
            and (row[0] or row[1] == "cancelled")
        )
    finally:
        db.close()


def _mark_cancelled(db: Session, run: ResearchAgentRun) -> None:
    for node in run.nodes:
        if node.status in {"pending", "running"}:
            node.status = "cancelled"
            node.completed_at = _utcnow()
    run.cancel_requested = True
    run.status = "cancelled"
    run.completed_at = _utcnow()
    db.commit()


def _fail_run(
    db: Session,
    run: ResearchAgentRun,
    message: str,
    *,
    node: ResearchAgentNode | None = None,
) -> None:
    if node is not None:
        node.status = "failed"
        node.error_message = message[:2000]
        node.completed_at = _utcnow()
    run.status = "failed"
    run.error_message = message[:4000]
    run.completed_at = _utcnow()
    db.commit()


def _select_ready_node(run: ResearchAgentRun) -> ResearchAgentNode | None:
    node_by_key = {node.node_key: node for node in run.nodes}
    for node in run.nodes:
        if node.status not in {"pending", "retry"}:
            continue
        dependencies = _json_load(node.dependencies_json, [])
        if all(
            node_by_key.get(dependency) is not None
            and node_by_key[dependency].status == "completed"
            for dependency in dependencies
        ):
            return node
    return None


def _has_unfinished_nodes(run: ResearchAgentRun) -> bool:
    return any(node.status in {"pending", "retry", "running"} for node in run.nodes)


def _report_sources(db: Session, run_id: str) -> list[ResearchAgentEvidence]:
    return (
        db.query(ResearchAgentEvidence)
        .filter(
            ResearchAgentEvidence.run_id == run_id,
            ResearchAgentEvidence.is_valid.is_(True),
        )
        .order_by(ResearchAgentEvidence.created_at.asc())
        .all()
    )


def _source_label(evidence: ResearchAgentEvidence) -> str:
    if evidence.source_type == "local_full_text":
        return (
            f"{evidence.title or '本地论文'}"
            f"（paper_id={evidence.paper_id}, page={evidence.page_number}, "
            f"chunk_id={evidence.chunk_id}）"
        )
    return (
        f"{evidence.title or 'arXiv 论文'}"
        f"（arXiv:{evidence.arxiv_id}，仅摘要，无全文页码）"
    )


def _fallback_report(
    run: ResearchAgentRun,
    evidence: list[ResearchAgentEvidence],
) -> ReportModelOutput:
    local_count = sum(
        item.source_type == "local_full_text" for item in evidence
    )
    external_count = sum(
        item.source_type == "external_arxiv_abstract" for item in evidence
    )
    sections = []
    for index, item in enumerate(evidence[:12], start=1):
        sections.append(
            {
                "heading": f"证据 {index}：{item.title or '未命名来源'}",
                "content": (
                    f"{item.excerpt}\n\n来源：{_source_label(item)}。"
                ),
                "citation_ids": [f"S{index}"],
            }
        )
    return ReportModelOutput(
        title=f"研究报告：{run.topic or run.goal[:120]}",
        executive_summary=(
            f"本报告仅基于本次 Agent 运行中通过运行时校验的 {len(evidence)} 条来源"
            f"生成，其中本地全文 {local_count} 条、arXiv 摘要 {external_count} 条。"
            "外部摘要不提供全文页码或 Chunk 证据。"
        ),
        sections=sections,
        limitations=(
            "报告没有使用未检索到的知识补写论文事实。"
            "arXiv 来源仅作外部摘要证据；若本地证据不足，相关结论仍需回到全文核验。"
        ),
    )


def _report_markdown(
    report: ReportModelOutput,
    evidence: list[ResearchAgentEvidence],
) -> str:
    parts = [
        f"# {report.title}",
        "",
        report.executive_summary,
        "",
    ]
    for section in report.sections:
        citations = ", ".join(section.citation_ids)
        parts.extend(
            [
                f"## {section.heading}",
                "",
                section.content,
                "",
                f"引用：{citations}",
                "",
            ]
        )
    parts.extend(["## 证据来源", ""])
    for index, item in enumerate(evidence, start=1):
        parts.append(f"- [S{index}] {_source_label(item)}")
    if report.limitations:
        parts.extend(["", "## 局限", "", report.limitations])
    return "\n".join(parts).strip() + "\n"


def _llm_report(
    run: ResearchAgentRun,
    evidence: list[ResearchAgentEvidence],
) -> ReportModelOutput:
    source_text = "\n\n".join(
        (
            f"[S{index}] {_source_label(item)}\n"
            f"来源类型：{item.source_type}\n"
            f"原文/摘要：{item.excerpt}"
        )
        for index, item in enumerate(evidence, start=1)
    )
    settings = get_settings()
    client = get_llm_client()
    response = client.chat.completions.create(
        model=settings.llm_model_name,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是证据约束的科研报告生成器。只能使用给定来源，"
                    "不得把 arXiv 摘要伪装成全文页码证据。只返回合法 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"研究目标：{run.goal}\n\n"
                    f"研究主题：{run.topic}\n"
                    f"核心问题：{run.research_question}\n\n"
                    f"可用来源：\n{source_text}\n\n"
                    "输出结构："
                    '{"title":"...","executive_summary":"...",'
                    '"sections":[{"heading":"...","content":"...",'
                    '"citation_ids":["S1"]}],"limitations":"..."}。'
                    "每个事实性章节至少引用一个可用来源 ID。"
                ),
            },
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
        timeout=75,
    )
    content = response.choices[0].message.content
    if not content:
        raise AppError(
            "报告模型未返回内容。",
            code="AGENT_REPORT_EMPTY",
            status_code=502,
        )
    raw = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        content.strip(),
        flags=re.IGNORECASE,
    )
    report = ReportModelOutput.model_validate_json(raw)
    allowed = {f"S{index}" for index in range(1, len(evidence) + 1)}
    cited: set[str] = set()
    for section in report.sections:
        if not section.citation_ids:
            raise ValueError("Report section has no citations.")
        if any(citation not in allowed for citation in section.citation_ids):
            raise ValueError("Report cited an unknown source.")
        cited.update(section.citation_ids)
    if not cited:
        raise ValueError("Report did not cite an evidence source.")
    return report


def _finalize_run(db: Session, run: ResearchAgentRun) -> None:
    db.refresh(run)
    if run.cancel_requested or run.status == "cancelled":
        _mark_cancelled(db, run)
        return
    evidence = _report_sources(db, run.id)
    if not evidence:
        _fail_run(
            db,
            run,
            "没有通过运行时校验的有效来源，报告不能标记为 completed。",
        )
        return

    report: ReportModelOutput | None = None
    settings = get_settings()
    if settings.llm_api_key and run.llm_calls_used < run.max_llm_calls:
        run.llm_calls_used += 1
        db.commit()
        try:
            report = _llm_report(run, evidence)
        except Exception:
            report = None
    if report is None:
        report = _fallback_report(run, evidence)

    markdown = _report_markdown(report, evidence)
    source_payload = [
        {
            "source_id": f"S{index}",
            "source_type": item.source_type,
            "paper_id": item.paper_id,
            "page_number": item.page_number,
            "chunk_id": item.chunk_id,
            "arxiv_id": item.arxiv_id,
            "title": item.title,
            "content_hash": item.content_hash,
            "source_url": item.source_url,
        }
        for index, item in enumerate(evidence, start=1)
    ]
    structured = {
        "report": report.model_dump(mode="json"),
        "markdown": markdown,
        "sources": source_payload,
        "plan_version": run.plan_version,
        "replan_count": run.replan_count,
    }
    db.add(
        ResearchAgentArtifact(
            run_id=run.id,
            artifact_type="final_report",
            content_json=_json_dump(structured),
            valid_source_count=len(evidence),
        )
    )
    run.status = "completed"
    run.error_message = None
    run.completed_at = _utcnow()
    db.commit()


def _execute_node(
    db: Session,
    run: ResearchAgentRun,
    node: ResearchAgentNode,
    *,
    registry: ToolRegistry,
    session_factory: Callable[[], Session],
) -> tuple[bool, dict[str, Any] | None, str | None, bool]:
    try:
        spec = registry.get(node.tool_name or "")
    except ToolExecutionError as exc:
        node.status = "failed"
        node.error_message = str(exc)
        node.completed_at = _utcnow()
        _fail_run(db, run, str(exc), node=node)
        return False, None, str(exc), False

    arguments = _json_load(node.input_json, {})
    last_error: str | None = None
    retryable = False
    max_attempts = min(spec.max_attempts, node.retry_limit + 1)
    node.status = "running"
    node.started_at = node.started_at or _utcnow()
    db.commit()

    for attempt in range(1, max_attempts + 1):
        if _is_cancel_requested(run.id, session_factory):
            break
        db.refresh(run)
        if run.tool_calls_used >= run.max_tool_calls:
            _fail_run(
                db,
                run,
                "工具调用预算已耗尽。",
                node=node,
            )
            return False, None, "工具调用预算已耗尽。", False
        tool_call_count = (
            db.query(ResearchAgentToolCall)
            .filter(
                ResearchAgentToolCall.run_id == run.id,
                ResearchAgentToolCall.tool_name == node.tool_name,
            )
            .count()
        )
        if tool_call_count >= spec.max_calls_per_run:
            _fail_run(
                db,
                run,
                f"工具 {node.tool_name} 已达到单任务调用上限。",
                node=node,
            )
            return False, None, "单工具调用预算已耗尽。", False
        if (
            node.tool_name == "search_arxiv"
            and run.arxiv_requests_used >= run.max_arxiv_requests
        ):
            _fail_run(
                db,
                run,
                "arXiv 网络请求预算已耗尽。",
                node=node,
            )
            return False, None, "arXiv 网络请求预算已耗尽。", False
        if (
            spec.llm_calls_per_execution
            and run.llm_calls_used + spec.llm_calls_per_execution
            > run.max_llm_calls
        ):
            _fail_run(
                db,
                run,
                "LLM 调用预算已耗尽。",
                node=node,
            )
            return False, None, "LLM 调用预算已耗尽。", False

        run.tool_calls_used += 1
        if node.tool_name == "search_arxiv":
            run.arxiv_requests_used += 1
        run.llm_calls_used += spec.llm_calls_per_execution
        call = ResearchAgentToolCall(
            run_id=run.id,
            node_id=node.id,
            tool_name=node.tool_name or "",
            attempt=attempt,
            arguments_summary_json=_json_dump(arguments),
            status="running",
            started_at=_utcnow(),
        )
        db.add(call)
        db.commit()
        started = time.monotonic()
        try:
            result = registry.execute(
                node.tool_name or "",
                arguments,
                ToolContext(
                    db=db,
                    run_id=run.id,
                    node_id=node.id,
                    cancel_check=lambda: _is_cancel_requested(
                        run.id,
                        session_factory,
                    ),
                ),
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            if duration_ms > spec.timeout * 1000:
                raise ToolExecutionError(
                    f"工具执行耗时 {duration_ms}ms，超过声明的 {spec.timeout}s 上限。",
                    code="AGENT_TOOL_TIMEOUT",
                    retryable=True,
                )
            valid_evidence = 0
            for raw_source in result.get("sources", []):
                try:
                    source = SourceCandidate.model_validate(raw_source)
                except ValidationError:
                    continue
                if _store_evidence(
                    db,
                    run_id=run.id,
                    node_id=node.id,
                    tool_call_id=call.id,
                    source=source,
                ):
                    valid_evidence += 1
            call.status = "completed"
            call.duration_ms = duration_ms
            call.completed_at = _utcnow()
            call.result_summary_json = _json_dump(
                {
                    **_summarize_tool_result(result),
                    "validated_evidence_count": valid_evidence,
                }
            )
            node.status = "completed"
            node.output_summary_json = call.result_summary_json
            node.error_message = None
            node.completed_at = _utcnow()
            db.commit()
            return True, result, None, True
        except ToolExecutionError as exc:
            db.rollback()
            call = db.get(ResearchAgentToolCall, call.id)
            if call is not None:
                call.status = "failed"
                call.duration_ms = int((time.monotonic() - started) * 1000)
                call.error_message = str(exc)[:2000]
                call.completed_at = _utcnow()
            node.retry_count = attempt
            last_error = str(exc)
            retryable = exc.retryable
            db.commit()
            if not exc.retryable or attempt >= max_attempts:
                break
        except Exception as exc:
            db.rollback()
            call = db.get(ResearchAgentToolCall, call.id)
            if call is not None:
                call.status = "failed"
                call.duration_ms = int((time.monotonic() - started) * 1000)
                call.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                call.completed_at = _utcnow()
            node.retry_count = attempt
            last_error = f"{type(exc).__name__}: {exc}"
            retryable = True
            db.commit()
            if attempt >= max_attempts:
                break

    if _is_cancel_requested(run.id, session_factory):
        return False, None, "cancelled", False
    _fail_run(
        db,
        run,
        last_error or "工具执行失败。",
        node=node,
    )
    return False, None, last_error, retryable


def execute_research_run(
    run_id: str,
    session_factory: Callable[[], Session],
    *,
    registry: ToolRegistry = TOOL_REGISTRY,
    planner: ResearchPlanner | None = None,
) -> None:
    db = session_factory()
    try:
        run = db.get(ResearchAgentRun, run_id)
        if run is None or run.status == "cancelled":
            return
        if run.status not in {"pending", "running"}:
            return
        planner = planner or ResearchPlanner(registry=registry)
        run.status = "running"
        run.started_at = run.started_at or _utcnow()
        run.error_message = None
        db.commit()
        budget = _budget_from_run(run)

        settings = get_settings()
        if settings.llm_api_key and run.max_llm_calls > 0:
            run.llm_calls_used += 1
            db.commit()
        try:
            plan = planner.create_plan(run.goal, budget)
        except PlanValidationError as exc:
            _fail_run(db, run, f"计划验证失败：{exc}")
            return
        except Exception as exc:
            _fail_run(db, run, f"计划生成失败：{type(exc).__name__}: {exc}")
            return
        if _is_cancel_requested(run.id, session_factory):
            _mark_cancelled(db, run)
            return
        run.topic = plan.topic
        run.research_question = plan.research_question
        run.time_range = plan.time_range
        run.analysis_dimensions_json = _json_dump(plan.analysis_dimensions)
        run.plan_version = plan.version
        _persist_plan_nodes(db, run, plan)
        db.commit()

        while True:
            db.refresh(run)
            if _is_cancel_requested(run.id, session_factory):
                _mark_cancelled(db, run)
                return
            elapsed = (
                (_utcnow() - run.started_at).total_seconds()
                if run.started_at
                else 0
            )
            if elapsed > run.max_execution_seconds:
                _fail_run(db, run, "总执行时间预算已耗尽。")
                return
            if not _has_unfinished_nodes(run):
                _finalize_run(db, run)
                return
            node = _select_ready_node(run)
            if node is None:
                _fail_run(
                    db,
                    run,
                    "没有依赖已满足的 ready 节点，计划可能被失败依赖阻塞。",
                )
                return
            success, result, error, _retryable = _execute_node(
                db,
                run,
                node,
                registry=registry,
                session_factory=session_factory,
            )
            if not success:
                if _is_cancel_requested(run.id, session_factory):
                    _mark_cancelled(db, run)
                return

            db.refresh(run)
            if (
                node.tool_name == "search_local_papers"
                and result is not None
                and run.replan_count < 1
            ):
                old_plan_payload = _json_load(run.plan_json, {})
                try:
                    old_plan = ValidatedPlan.model_validate(old_plan_payload)
                except ValidationError:
                    old_plan = plan
                replanned = planner.controlled_replan(
                    old_plan,
                    budget=budget,
                    source_node_id=node.node_key,
                    result=result,
                )
                if replanned is not None:
                    new_ids = {
                        item.id for item in replanned.nodes
                    } - {item.id for item in old_plan.nodes}
                    _persist_plan_nodes(
                        db,
                        run,
                        replanned,
                        new_node_ids=new_ids,
                    )
                    run.replan_count += 1
                    db.commit()
                    plan = replanned
    finally:
        db.close()
