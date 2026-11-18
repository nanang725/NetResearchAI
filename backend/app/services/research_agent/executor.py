from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
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
from app.services.llm_json import (
    LLMJSONError,
    classify_llm_exception,
    finish_reason,
    parse_json_object,
    repair_messages,
    response_content,
    safe_diagnostics,
)
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
from app.services.research_agent.bindings import (
    BindingResolutionError,
    resolve_node_input,
)
from app.services.research_agent.worker import (
    LeaseHeartbeat,
    LeaseLostError,
    RunLease,
    assert_lease,
    reconcile_claimed_run,
)


NODE_INPUT_ARTIFACT = "node_input"
NODE_OUTPUT_ARTIFACT = "node_output"
NODE_ERROR_ARTIFACT = "node_error"
STRATEGY_DECISION_ARTIFACT = "strategy_decision"
LLM_REPORT_RESERVED_CALLS = 1


class ReportGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []


def _utcnow() -> datetime:
    return datetime.utcnow()


def _install_worker_fence(
    db: Session,
    lease: RunLease,
    heartbeat: LeaseHeartbeat,
) -> None:
    original_commit = db.commit

    def fenced_commit() -> None:
        heartbeat.assert_active()
        assert_lease(
            db,
            lease,
            allowed_statuses=(
                "running",
                "cancelling",
                "completed",
                "completed_with_warnings",
                "partial",
                "failed",
                "cancelled",
            ),
        )
        original_commit()

    db.commit = fenced_commit  # type: ignore[method-assign]


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _dynamic_llm_call_consumer(db: Session, run: ResearchAgentRun):
    def consume() -> None:
        db.refresh(run)
        if (
            run.llm_calls_used
            >= run.max_llm_calls - LLM_REPORT_RESERVED_CALLS
        ):
            raise ToolExecutionError(
                (
                    "LLM call budget cannot consume the final report "
                    f"reserve. Used: {run.llm_calls_used}, limit: "
                    f"{run.max_llm_calls}."
                ),
                code="AGENT_REPORT_LLM_RESERVE",
                retryable=False,
                category="validation_error",
            )
        run.llm_calls_used += 1
        db.commit()

    return consume


def _store_node_artifact(
    db: Session,
    *,
    run_id: str,
    artifact_type: str,
    payload: dict[str, Any],
    valid_source_count: int = 0,
) -> ResearchAgentArtifact:
    artifact = ResearchAgentArtifact(
        run_id=run_id,
        artifact_type=artifact_type,
        content_json=_json_dump(payload),
        valid_source_count=valid_source_count,
    )
    db.add(artifact)
    return artifact


def _store_strategy_decision(
    db: Session,
    *,
    run_id: str,
    node: ResearchAgentNode,
    strategy: str,
    outcome: str,
    reason: str,
    fallback_tool: str | None = None,
    error_category: str | None = None,
) -> None:
    _store_node_artifact(
        db,
        run_id=run_id,
        artifact_type=STRATEGY_DECISION_ARTIFACT,
        payload={
            "node_id": node.id,
            "node_key": node.node_key,
            "strategy": strategy,
            "outcome": outcome,
            "reason": reason,
            "fallback_tool": fallback_tool,
            "error_category": error_category,
        },
    )


def _load_completed_node_outputs(
    db: Session,
    run_id: str,
) -> dict[str, dict[str, Any]]:
    rows = (
        db.query(ResearchAgentArtifact)
        .filter(
            ResearchAgentArtifact.run_id == run_id,
            ResearchAgentArtifact.artifact_type == NODE_OUTPUT_ARTIFACT,
        )
        .order_by(ResearchAgentArtifact.created_at.asc())
        .all()
    )
    outputs: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _json_load(row.content_json, {})
        node_key = payload.get("node_key")
        output = payload.get("output")
        if isinstance(node_key, str) and isinstance(output, dict):
            outputs[node_key] = output
    return outputs


def _plan_node_for_run(
    run: ResearchAgentRun,
    node: ResearchAgentNode,
) -> Any:
    payload = _json_load(run.plan_json, {})
    try:
        plan = ValidatedPlan.model_validate(payload)
    except ValidationError:
        return None
    return next((item for item in plan.nodes if item.id == node.node_key), None)


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
        max_replans=run.max_replans,
        max_execution_seconds=run.max_execution_seconds,
    )


def _apply_planner_llm_usage(
    run: ResearchAgentRun,
    planner: Any,
    llm_calls_before: int,
) -> None:
    reported_calls = getattr(planner, "last_llm_calls_used", None)
    if reported_calls is None:
        # Test or custom planners that do not own an LLM must not be charged
        # as if they made a model request.
        actual_calls = 0
    else:
        actual_calls = max(0, int(reported_calls))
    run.llm_calls_used = min(
        run.max_llm_calls,
        llm_calls_before + actual_calls,
    )


def _plan_payload(plan: ValidatedPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def persist_plan_nodes(
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


def _mark_dependency_blocked_nodes(
    db: Session,
    run: ResearchAgentRun,
) -> list[str]:
    node_by_key = {node.node_key: node for node in run.nodes}
    blocked: list[str] = []
    changed = True
    while changed:
        changed = False
        for node in run.nodes:
            if node.status not in {"pending", "retry"}:
                continue
            dependencies = _json_load(node.dependencies_json, [])
            if not any(
                node_by_key.get(dependency) is None
                or node_by_key[dependency].status
                in {"failed", "skipped", "blocked", "cancelled"}
                for dependency in dependencies
            ):
                continue
            node.status = "blocked"
            node.error_message = "依赖节点未成功完成，当前节点不能执行。"
            node.completed_at = _utcnow()
            _store_strategy_decision(
                db,
                run_id=run.id,
                node=node,
                strategy="dependency_block",
                outcome="blocked",
                reason=node.error_message,
            )
            _store_node_artifact(
                db,
                run_id=run.id,
                artifact_type=NODE_ERROR_ARTIFACT,
                payload={
                    "node_id": node.id,
                    "node_key": node.node_key,
                    "tool_name": node.tool_name,
                    "error": {
                        "category": "internal_error",
                        "code": "AGENT_DEPENDENCY_BLOCKED",
                        "message": node.error_message,
                        "retryable": False,
                    },
                },
            )
            blocked.append(node.node_key)
            changed = True
    if blocked:
        db.commit()
    return blocked


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


# Retained for v0.4 module-surface compatibility. Runtime execution always
# uses `_llm_report_bounded` so JSON repair, deadlines and budgets are enforced.
def _legacy_llm_report(
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


def _llm_report_bounded(
    run: ResearchAgentRun,
    evidence: list[ResearchAgentEvidence],
    *,
    max_attempts: int,
    consume_llm_call: Callable[[], None],
    deadline: datetime | None = None,
) -> tuple[ReportModelOutput, list[dict[str, Any]]]:
    source_text = "\n\n".join(
        (
            f"[S{index}] {_source_label(item)}\n"
            f"source_type: {item.source_type}\n"
            f"excerpt: {item.excerpt}"
        )
        for index, item in enumerate(evidence, start=1)
    )
    settings = get_settings()
    client = get_llm_client()
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You produce evidence-grounded research reports. Use only "
                "the supplied sources. Never present an arXiv abstract as "
                "full-text page evidence. Return one valid JSON object only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Research goal: {run.goal}\n\n"
                f"Topic: {run.topic}\n"
                f"Research question: {run.research_question}\n\n"
                f"Available sources:\n{source_text}\n\n"
                "Required structure:\n"
                '{"title":"...","executive_summary":"...",'
                '"sections":[{"heading":"...","content":"...",'
                '"citation_ids":["S1"]}],"limitations":"..."}\n'
                "Each factual section must cite at least one supplied "
                "source ID."
            ),
        },
    ]
    schema_text = json.dumps(
        ReportModelOutput.model_json_schema(),
        ensure_ascii=False,
    )
    allowed = {f"S{index}" for index in range(1, len(evidence) + 1)}
    diagnostics: list[dict[str, Any]] = []

    for attempt in range(1, max_attempts + 1):
        timeout = 75
        if deadline is not None:
            remaining_seconds = (
                deadline - datetime.utcnow()
            ).total_seconds()
            if remaining_seconds <= 2:
                diagnostics.append(
                    {
                        "stage": "final_report",
                        "attempt": attempt,
                        "failure_kind": "run_time_budget_exhausted",
                        "remaining_seconds": max(0, int(remaining_seconds)),
                    }
                )
                raise ReportGenerationError(
                    "Run time budget is insufficient for final report generation.",
                    code="AGENT_REPORT_TIME_BUDGET_EXHAUSTED",
                    diagnostics=diagnostics,
                )
            timeout = min(timeout, max(1, int(remaining_seconds - 1)))
        consume_llm_call()
        try:
            response = client.chat.completions.create(
                model=settings.llm_model_name,
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=timeout,
            )
        except Exception as exc:
            diagnostics.append(
                safe_diagnostics(
                    stage="final_report",
                    attempt=attempt,
                    content=None,
                    finish_reason=None,
                    failure_kind=classify_llm_exception(exc),
                    error=exc,
                )
            )
            raise ReportGenerationError(
                "Final report LLM request failed.",
                code="AGENT_REPORT_LLM_ERROR",
                diagnostics=diagnostics,
            ) from exc

        content: str | None = None
        try:
            content = response_content(response)
            report = ReportModelOutput.model_validate(
                parse_json_object(content)
            )
            cited: set[str] = set()
            for section in report.sections:
                if not section.citation_ids:
                    raise ValueError("Report section has no citations.")
                if any(
                    citation not in allowed
                    for citation in section.citation_ids
                ):
                    raise ValueError(
                        "Report cited an unknown evidence source."
                    )
                cited.update(section.citation_ids)
            if not cited:
                raise ValueError(
                    "Report did not cite an evidence source."
                )
            return report, diagnostics
        except (LLMJSONError, ValidationError) as exc:
            failure_kind = (
                exc.kind
                if isinstance(exc, LLMJSONError)
                else "schema_validation_error"
            )
            diagnostics.append(
                safe_diagnostics(
                    stage="final_report",
                    attempt=attempt,
                    content=content,
                    finish_reason=finish_reason(response),
                    failure_kind=failure_kind,
                    error=exc,
                )
            )
            if attempt < max_attempts:
                messages = repair_messages(
                    invalid_content=content or "",
                    schema_text=schema_text,
                    instruction=(
                        "Preserve every substantive claim and use only the "
                        "S-prefixed citation IDs that already appeared in "
                        "the malformed response."
                    ),
                )
                continue
            raise ReportGenerationError(
                "Final report structured output remained invalid.",
                code=(
                    "AGENT_REPORT_SCHEMA_ERROR"
                    if failure_kind == "schema_validation_error"
                    else (
                        "AGENT_REPORT_JSON_ERROR"
                        if failure_kind
                        in {"json_parse_error", "json_shape_error"}
                        else "AGENT_REPORT_RESPONSE_ERROR"
                    )
                ),
                diagnostics=diagnostics,
            ) from exc
        except ValueError as exc:
            diagnostics.append(
                safe_diagnostics(
                    stage="final_report",
                    attempt=attempt,
                    content=content,
                    finish_reason=finish_reason(response),
                    failure_kind="citation_validation_error",
                    error=exc,
                )
            )
            raise ReportGenerationError(
                "Final report citation validation failed.",
                code="AGENT_REPORT_CITATION_ERROR",
                diagnostics=diagnostics,
            ) from exc

    raise ReportGenerationError(
        "Final report LLM produced no usable result.",
        code="AGENT_REPORT_EMPTY",
        diagnostics=diagnostics,
    )


def _finalize_run(db: Session, run: ResearchAgentRun) -> None:
    db.refresh(run)
    if run.cancel_requested or run.status == "cancelled":
        _mark_cancelled(db, run)
        return
    evidence = _report_sources(db, run.id)
    if not evidence:
        has_nonempty_output = False
        for row in (
            db.query(ResearchAgentArtifact)
            .filter(
                ResearchAgentArtifact.run_id == run.id,
                ResearchAgentArtifact.artifact_type == NODE_OUTPUT_ARTIFACT,
            )
            .all()
        ):
            payload = _json_load(row.content_json, {})
            output = payload.get("output", {})
            data = output.get("data", {})
            if (
                isinstance(data, dict)
                and isinstance(data.get("result_count"), int)
                and data["result_count"] > 0
            ):
                has_nonempty_output = True
                break
        run.status = "partial" if has_nonempty_output else "failed"
        run.error_message = (
            "未获得通过运行时校验的有效来源；"
            f"任务结束状态为 {run.status}。"
        )
        run.completed_at = _utcnow()
        db.commit()
        return

    report: ReportModelOutput | None = None
    report_generation_mode = "fallback"
    report_diagnostics: list[dict[str, Any]] = []
    report_attempts = 0
    settings = get_settings()
    if settings.llm_api_key:
        remaining = max(0, run.max_llm_calls - run.llm_calls_used)
        if remaining:
            def consume_report_call() -> None:
                nonlocal report_attempts
                db.refresh(run)
                if run.llm_calls_used >= run.max_llm_calls:
                    raise ReportGenerationError(
                        "Final report LLM budget was exhausted.",
                        code="AGENT_REPORT_LLM_BUDGET_EXHAUSTED",
                        diagnostics=report_diagnostics,
                    )
                run.llm_calls_used += 1
                report_attempts += 1
                db.commit()

            report_deadline = (
                run.started_at
                + timedelta(seconds=run.max_execution_seconds)
                if run.started_at
                else None
            )
            try:
                report, report_diagnostics = _llm_report_bounded(
                    run,
                    evidence,
                    max_attempts=min(2, remaining),
                    consume_llm_call=consume_report_call,
                    deadline=report_deadline,
                )
                report_generation_mode = "llm"
            except ReportGenerationError as exc:
                report_diagnostics = exc.diagnostics or [
                    {
                        "stage": "final_report",
                        "failure_kind": "generation_error",
                        "code": exc.code,
                    }
                ]
            except Exception as exc:
                report_diagnostics = [
                    safe_diagnostics(
                        stage="final_report",
                        attempt=max(1, report_attempts),
                        content=None,
                        finish_reason=None,
                        failure_kind=classify_llm_exception(exc),
                        error=exc,
                    )
                ]
        else:
            report_diagnostics = [
                {
                    "stage": "final_report",
                    "failure_kind": "budget_exhausted",
                    "code": "AGENT_REPORT_LLM_BUDGET_EXHAUSTED",
                    "remaining_calls": 0,
                }
            ]
    if report is None:
        report = _fallback_report(run, evidence)
    if report_diagnostics:
        db.add(
            ResearchAgentArtifact(
                run_id=run.id,
                artifact_type="llm_diagnostics",
                content_json=_json_dump(
                    {
                        "stage": "final_report",
                        "generation_mode": report_generation_mode,
                        "attempts": report_attempts,
                        "diagnostics": report_diagnostics,
                    }
                ),
                valid_source_count=0,
            )
        )

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
        "generation_mode": report_generation_mode,
    }
    db.add(
        ResearchAgentArtifact(
            run_id=run.id,
            artifact_type="final_report",
            content_json=_json_dump(structured),
            valid_source_count=len(evidence),
        )
    )
    warning_nodes = [
        node for node in run.nodes
        if node.status in {"failed", "skipped"}
    ]
    blocked_nodes = [
        node for node in run.nodes
        if node.status == "blocked"
    ]
    if blocked_nodes:
        run.status = "partial"
    elif warning_nodes or report_diagnostics:
        run.status = "completed_with_warnings"
    else:
        run.status = "completed"
    run.error_message = None
    run.completed_at = _utcnow()
    db.commit()


def _execute_fallback_tool(
    db: Session,
    run: ResearchAgentRun,
    node: ResearchAgentNode,
    planned_node: Any,
    *,
    registry: ToolRegistry,
    session_factory: Callable[[], Session],
) -> tuple[bool, dict[str, Any] | None, str | None]:
    fallback_name = planned_node.fallback_tool
    if not fallback_name:
        return False, None, "替代工具未配置。"
    try:
        spec = registry.get(fallback_name)
    except ToolExecutionError as exc:
        return False, None, str(exc)
    arguments = planned_node.fallback_arguments
    if run.tool_calls_used >= run.max_tool_calls:
        return False, None, "工具调用预算已耗尽，不能执行替代工具。"
    call_count = (
        db.query(ResearchAgentToolCall)
        .filter(
            ResearchAgentToolCall.run_id == run.id,
            ResearchAgentToolCall.tool_name == fallback_name,
                ResearchAgentToolCall.status != "interrupted",
        )
        .count()
    )
    if call_count >= spec.max_calls_per_run:
        return False, None, "替代工具已达到单任务调用上限。"
    if (
        fallback_name == "search_arxiv"
        and run.arxiv_requests_used >= run.max_arxiv_requests
    ):
        return False, None, "arXiv 请求预算已耗尽，不能执行替代工具。"
    required_calls = (
        1
        if spec.dynamic_llm_calls and spec.llm_calls_per_execution
        else spec.llm_calls_per_execution
    )
    if (
        required_calls
        and run.llm_calls_used
        + required_calls
        + LLM_REPORT_RESERVED_CALLS
        > run.max_llm_calls
    ):
        return False, None, "LLM 预算已耗尽，不能执行替代工具。"

    run.tool_calls_used += 1
    if fallback_name == "search_arxiv":
        run.arxiv_requests_used += 1
    if not spec.dynamic_llm_calls:
        run.llm_calls_used += spec.llm_calls_per_execution
    call = ResearchAgentToolCall(
        run_id=run.id,
        node_id=node.id,
        tool_name=fallback_name,
        attempt=node.retry_count + 1,
        arguments_summary_json=_json_dump(arguments),
        status="running",
        started_at=_utcnow(),
    )
    db.add(call)
    db.commit()
    started = time.monotonic()
    try:
        result = registry.execute(
            fallback_name,
            arguments,
            ToolContext(
                db=db,
                run_id=run.id,
                node_id=node.id,
                cancel_check=lambda: _is_cancel_requested(
                    run.id,
                    session_factory,
                ),
                consume_llm_call=(
                    _dynamic_llm_call_consumer(db, run)
                    if spec.dynamic_llm_calls
                    else None
                ),
            ),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if duration_ms > spec.timeout * 1000:
            raise ToolExecutionError(
                f"替代工具执行耗时 {duration_ms}ms，超过 {spec.timeout}s 上限。",
                code="AGENT_TOOL_TIMEOUT",
                retryable=False,
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
                "tool_status": result.get("status"),
                "validated_evidence_count": valid_evidence,
                "fallback_tool": fallback_name,
            }
        )
        _store_node_artifact(
            db,
            run_id=run.id,
            artifact_type=NODE_OUTPUT_ARTIFACT,
            payload={
                "node_id": node.id,
                "node_key": node.node_key,
                "tool_name": fallback_name,
                "requested_tool_name": node.tool_name,
                "output": result,
            },
            valid_source_count=valid_evidence,
        )
        _store_strategy_decision(
            db,
            run_id=run.id,
            node=node,
            strategy="fallback_tool",
            outcome="succeeded",
            reason="主工具失败后使用已注册只读替代工具。",
            fallback_tool=fallback_name,
        )
        node.status = "completed"
        node.error_message = None
        node.output_summary_json = call.result_summary_json
        node.completed_at = _utcnow()
        db.commit()
        return True, result, None
    except LeaseLostError:
        raise
    except (ToolExecutionError, Exception) as exc:
        db.rollback()
        call = db.get(ResearchAgentToolCall, call.id)
        category = (
            exc.category
            if isinstance(exc, ToolExecutionError)
            else "internal_error"
        )
        if call is not None:
            call.status = "failed"
            call.duration_ms = int((time.monotonic() - started) * 1000)
            call.error_message = str(exc)[:2000]
            call.completed_at = _utcnow()
            call.result_summary_json = _json_dump(
                {
                    "success": False,
                    "status": "error",
                    "error_category": category,
                    "error_code": "AGENT_FALLBACK_TOOL_FAILED",
                    "retryable": False,
                }
            )
        _store_strategy_decision(
            db,
            run_id=run.id,
            node=node,
            strategy="fallback_tool",
            outcome="failed",
            reason=str(exc),
            fallback_tool=fallback_name,
            error_category=category,
        )
        node.error_message = str(exc)[:2000]
        db.commit()
        return False, None, str(exc)


def _execute_node(
    db: Session,
    run: ResearchAgentRun,
    node: ResearchAgentNode,
    *,
    registry: ToolRegistry,
    session_factory: Callable[[], Session],
    completed_outputs: dict[str, dict[str, Any]],
) -> tuple[bool, dict[str, Any] | None, str | None, bool]:
    try:
        spec = registry.get(node.tool_name or "")
    except ToolExecutionError as exc:
        node.status = "failed"
        node.error_message = str(exc)
        node.completed_at = _utcnow()
        _fail_run(db, run, str(exc), node=node)
        return False, None, str(exc), False

    original_arguments = _json_load(node.input_json, {})
    planned_node = _plan_node_for_run(run, node)
    bindings = planned_node.input_bindings if planned_node is not None else {}
    dependencies = _json_load(node.dependencies_json, [])
    node_statuses = {item.node_key: item.status for item in run.nodes}
    try:
        resolved = resolve_node_input(
            tool_name=node.tool_name or "",
            node_id=node.node_key,
            original_arguments=original_arguments,
            bindings=bindings,
            dependencies=dependencies,
            node_statuses=node_statuses,
            completed_outputs=completed_outputs,
            registry=registry,
        )
        arguments = resolved.arguments
        binding_sources = [
            source.as_dict() for source in resolved.binding_sources
        ]
    except BindingResolutionError as exc:
        node.status = "failed"
        node.error_message = str(exc)
        node.started_at = node.started_at or _utcnow()
        node.completed_at = _utcnow()
        _store_node_artifact(
            db,
            run_id=run.id,
            artifact_type=NODE_INPUT_ARTIFACT,
            payload={
                "node_id": node.id,
                "node_key": node.node_key,
                "tool_name": node.tool_name,
                "original_arguments": original_arguments,
                "resolved_arguments": None,
                "binding_sources": [],
                "error": {
                    "category": "validation_error",
                    "code": exc.code,
                    "message": str(exc),
                },
            },
        )
        _store_node_artifact(
            db,
            run_id=run.id,
            artifact_type=NODE_ERROR_ARTIFACT,
            payload={
                "node_id": node.id,
                "node_key": node.node_key,
                "tool_name": node.tool_name,
                "error": {
                    "category": "validation_error",
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": False,
                },
            },
        )
        _fail_run(db, run, f"数据绑定失败：{exc}", node=node)
        return False, None, str(exc), False

    _store_node_artifact(
        db,
        run_id=run.id,
        artifact_type=NODE_INPUT_ARTIFACT,
        payload={
            "node_id": node.id,
            "node_key": node.node_key,
            "tool_name": node.tool_name,
            "original_arguments": original_arguments,
            "resolved_arguments": arguments,
            "binding_sources": binding_sources,
        },
    )
    last_error: str | None = None
    retryable = False
    last_error_category = "internal_error"
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
                ResearchAgentToolCall.status != "interrupted",
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
        required_calls = (
            1
            if spec.dynamic_llm_calls and spec.llm_calls_per_execution
            else spec.llm_calls_per_execution
        )
        if (
            required_calls
            and run.llm_calls_used
            + required_calls
            + LLM_REPORT_RESERVED_CALLS
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
        if not spec.dynamic_llm_calls:
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
                    consume_llm_call=(
                        _dynamic_llm_call_consumer(db, run)
                        if spec.dynamic_llm_calls
                        else None
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
                    "tool_status": result.get("status"),
                    "validated_evidence_count": valid_evidence,
                }
            )
            _store_node_artifact(
                db,
                run_id=run.id,
                artifact_type=NODE_OUTPUT_ARTIFACT,
                payload={
                    "node_id": node.id,
                    "node_key": node.node_key,
                    "tool_name": node.tool_name,
                    "output": result,
                },
                valid_source_count=valid_evidence,
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
                call.result_summary_json = _json_dump(
                    {
                        "success": False,
                        "status": "error",
                        "error_category": exc.category,
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                        "error_details": exc.details,
                    }
                )
                call.completed_at = _utcnow()
            node.retry_count = attempt
            last_error = str(exc)
            retryable = exc.retryable
            last_error_category = exc.category
            db.commit()
            if not exc.retryable or attempt >= max_attempts:
                break
        except Exception as exc:
            if isinstance(exc, LeaseLostError):
                raise
            db.rollback()
            call = db.get(ResearchAgentToolCall, call.id)
            if call is not None:
                call.status = "failed"
                call.duration_ms = int((time.monotonic() - started) * 1000)
                call.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                call.result_summary_json = _json_dump(
                    {
                        "success": False,
                        "status": "error",
                        "error_category": "internal_error",
                        "error_code": "AGENT_TOOL_EXECUTION_FAILED",
                        "retryable": True,
                    }
                )
                call.completed_at = _utcnow()
            node.retry_count = attempt
            last_error = f"{type(exc).__name__}: {exc}"
            retryable = True
            last_error_category = "internal_error"
            db.commit()
            if attempt >= max_attempts:
                break

    if _is_cancel_requested(run.id, session_factory):
        return False, None, "cancelled", False

    strategy = (
        planned_node.failure_strategy
        if planned_node is not None
        else "retry_then_fail"
    )
    if strategy == "fallback_tool" and planned_node is not None:
        fallback_success, fallback_result, fallback_error = (
            _execute_fallback_tool(
                db,
                run,
                node,
                planned_node,
                registry=registry,
                session_factory=session_factory,
            )
        )
        if fallback_success:
            return True, fallback_result, None, True
        last_error = fallback_error or last_error
        last_error_category = "external_service_error"

    if strategy == "skip":
        node.status = "skipped"
        node.completed_at = _utcnow()
        node.error_message = last_error or "节点已跳过。"
        _store_strategy_decision(
            db,
            run_id=run.id,
            node=node,
            strategy="skip",
            outcome="skipped",
            reason=last_error or "非关键节点执行失败，按计划跳过。",
            error_category=last_error_category,
        )
        _store_node_artifact(
            db,
            run_id=run.id,
            artifact_type=NODE_ERROR_ARTIFACT,
            payload={
                "node_id": node.id,
                "node_key": node.node_key,
                "tool_name": node.tool_name,
                "error": {
                    "category": last_error_category,
                    "code": "AGENT_NODE_SKIPPED",
                    "message": last_error or "节点已跳过。",
                    "retryable": False,
                },
            },
        )
        db.commit()
        return False, None, last_error, True

    _fail_run(
        db,
        run,
        last_error or "工具执行失败。",
        node=node,
    )
    _store_node_artifact(
        db,
        run_id=run.id,
        artifact_type=NODE_ERROR_ARTIFACT,
        payload={
            "node_id": node.id,
            "node_key": node.node_key,
            "tool_name": node.tool_name,
            "error": {
                "category": last_error_category,
                "code": "AGENT_NODE_FAILED",
                "message": last_error or "工具执行失败。",
                "retryable": retryable,
            },
        },
    )
    db.commit()
    return False, None, last_error, retryable


def execute_research_run(
    run_id: str,
    session_factory: Callable[[], Session],
    *,
    registry: ToolRegistry = TOOL_REGISTRY,
    planner: ResearchPlanner | None = None,
    lease: RunLease | None = None,
    heartbeat: LeaseHeartbeat | None = None,
) -> None:
    db = session_factory()
    try:
        run = db.get(ResearchAgentRun, run_id)
        if run is None or run.status == "cancelled":
            return
        if run.status not in {"pending", "running", "cancelling"}:
            return
        if lease is not None:
            if heartbeat is None:
                raise ValueError("A heartbeat is required with an execution lease.")
            assert_lease(db, lease)
            reconcile_claimed_run(db, lease)
            db.refresh(run)
            _install_worker_fence(db, lease, heartbeat)
            if _is_cancel_requested(run.id, session_factory):
                _mark_cancelled(db, run)
                return
        planner = planner or ResearchPlanner(registry=registry)
        run.status = "running"
        run.started_at = run.started_at or _utcnow()
        run.error_message = None
        db.commit()
        budget = _budget_from_run(run)

        persisted_plan = _json_load(run.plan_json, None)
        if persisted_plan is not None and run.plan_version > 0:
            try:
                plan = ValidatedPlan.model_validate(persisted_plan)
            except ValidationError as exc:
                _fail_run(db, run, f"已确认计划无法读取：{exc}")
                return
        else:
            llm_calls_before_plan = run.llm_calls_used
            try:
                plan = planner.create_plan(run.goal, budget)
            except PlanValidationError as exc:
                _apply_planner_llm_usage(
                    run,
                    planner,
                    llm_calls_before_plan,
                )
                _fail_run(db, run, f"计划验证失败：{exc}")
                return
            except Exception as exc:
                _apply_planner_llm_usage(
                    run,
                    planner,
                    llm_calls_before_plan,
                )
                _fail_run(db, run, f"计划生成失败：{type(exc).__name__}: {exc}")
                return
            _apply_planner_llm_usage(
                run,
                planner,
                llm_calls_before_plan,
            )
            planner_diagnostics = getattr(
                planner,
                "last_llm_diagnostics",
                None,
            )
            if planner_diagnostics:
                _store_node_artifact(
                    db,
                    run_id=run.id,
                    artifact_type="llm_diagnostics",
                    payload={
                        "stage": "planner",
                        "generation_mode": plan.generated_by,
                        "attempts": (
                            planner_diagnostics.get("attempts", [])
                            if isinstance(planner_diagnostics, dict)
                            else []
                        ),
                        "diagnostics": (
                            planner_diagnostics
                            if isinstance(planner_diagnostics, dict)
                            else {}
                        ),
                    },
                )
            db.commit()
        if _is_cancel_requested(run.id, session_factory):
            _mark_cancelled(db, run)
            return
        run.topic = plan.topic
        run.research_question = plan.research_question
        run.time_range = plan.time_range
        run.analysis_dimensions_json = _json_dump(plan.analysis_dimensions)
        run.plan_version = plan.version
        persist_plan_nodes(db, run, plan)
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
                blocked = _mark_dependency_blocked_nodes(db, run)
                if blocked:
                    db.refresh(run)
                    _finalize_run(db, run)
                    return
                _fail_run(
                    db,
                    run,
                    "没有依赖已满足的 ready 节点，计划可能被失败依赖阻塞。",
                )
                return
            success, result, error, can_continue = _execute_node(
                db,
                run,
                node,
                registry=registry,
                session_factory=session_factory,
                completed_outputs=_load_completed_node_outputs(db, run.id),
            )
            if not success:
                if _is_cancel_requested(run.id, session_factory):
                    _mark_cancelled(db, run)
                if not can_continue:
                    return
                continue

            db.refresh(run)
            if (
                node.tool_name == "search_local_papers"
                and result is not None
                and run.replan_count < run.max_replans
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
                    if len(new_ids) > 3:
                        _store_node_artifact(
                            db,
                            run_id=run.id,
                            artifact_type="replan_decision",
                            payload={
                                "trigger": "search_result",
                                "old_plan_version": old_plan.version,
                                "new_plan_version": None,
                                "reason": "单次重规划新增节点数超过安全上限。",
                                "planner_source": replanned.generated_by,
                                "added_nodes": sorted(new_ids),
                                "accepted": False,
                            },
                        )
                        db.commit()
                        continue
                    persist_plan_nodes(
                        db,
                        run,
                        replanned,
                        new_node_ids=new_ids,
                    )
                    run.replan_count += 1
                    _store_node_artifact(
                        db,
                        run_id=run.id,
                        artifact_type="replan_decision",
                        payload={
                            "trigger": "search_result",
                            "old_plan_version": old_plan.version,
                            "new_plan_version": replanned.version,
                            "reason": ResearchPlanner.replan_reason(
                                old_plan,
                                source_node_id=node.node_key,
                                result=result,
                            ),
                            "planner_source": replanned.generated_by,
                            "added_nodes": sorted(new_ids),
                            "accepted": True,
                            "remaining_tool_calls": (
                                run.max_tool_calls - run.tool_calls_used
                            ),
                            "remaining_llm_calls": (
                                run.max_llm_calls - run.llm_calls_used
                            ),
                        },
                    )
                    _store_node_artifact(
                        db,
                        run_id=run.id,
                        artifact_type="plan_version",
                        payload={
                            "version": replanned.version,
                            "action": "replanned",
                            "change_reason": ResearchPlanner.replan_reason(
                                old_plan,
                                source_node_id=node.node_key,
                                result=result,
                            ),
                            "plan": replanned.model_dump(mode="json"),
                        },
                    )
                    db.commit()
                    plan = replanned
    finally:
        db.close()
