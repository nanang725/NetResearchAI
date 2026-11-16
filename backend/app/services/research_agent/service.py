from __future__ import annotations

import json
from datetime import datetime
from threading import Lock, Thread
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppError
from app.models.research_agent import (
    ResearchAgentArtifact,
    ResearchAgentEvidence,
    ResearchAgentNode,
    ResearchAgentRun,
    ResearchAgentToolCall,
)
from app.schemas.research_agent import AgentBudgetRequest
from app.services.research_agent.executor import execute_research_run


_THREADS: dict[str, Thread] = {}
_THREADS_LOCK = Lock()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def get_run_or_404(db: Session, run_id: str) -> ResearchAgentRun:
    run = db.get(ResearchAgentRun, run_id)
    if run is None:
        raise AppError(
            "研究任务不存在。",
            code="RESEARCH_AGENT_RUN_NOT_FOUND",
            status_code=404,
            details={"run_id": run_id},
        )
    return run


def create_research_run(
    db: Session,
    *,
    goal: str,
    budget: AgentBudgetRequest,
) -> ResearchAgentRun:
    run = ResearchAgentRun(
        goal=goal,
        topic="",
        research_question="",
        analysis_dimensions_json="[]",
        status="pending",
        plan_version=0,
        replan_count=0,
        max_nodes=budget.max_nodes,
        max_tool_calls=budget.max_tool_calls,
        max_llm_calls=budget.max_llm_calls,
        max_arxiv_requests=budget.max_arxiv_requests,
        max_execution_seconds=budget.max_execution_seconds,
        tool_calls_used=0,
        llm_calls_used=0,
        arxiv_requests_used=0,
        cancel_requested=False,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def start_research_run_thread(
    run_id: str,
    session_factory: Callable[[], Session],
) -> None:
    def worker() -> None:
        try:
            execute_research_run(run_id, session_factory)
        except Exception as exc:
            db = session_factory()
            try:
                run = db.get(ResearchAgentRun, run_id)
                if run is not None and run.status not in {
                    "completed",
                    "cancelled",
                }:
                    run.status = "failed"
                    run.error_message = (
                        f"Agent 后台执行异常：{type(exc).__name__}: {exc}"
                    )[:4000]
                    run.completed_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
        finally:
            with _THREADS_LOCK:
                _THREADS.pop(run_id, None)

    with _THREADS_LOCK:
        existing = _THREADS.get(run_id)
        if existing is not None and existing.is_alive():
            return
        thread = Thread(
            target=worker,
            name=f"research-agent-{run_id[:8]}",
            daemon=True,
        )
        _THREADS[run_id] = thread
        thread.start()


def start_research_run_from_session(db: Session, run_id: str) -> None:
    bind = db.get_bind()
    session_factory = sessionmaker(
        bind=bind,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    start_research_run_thread(run_id, session_factory)


def cancel_research_run(
    db: Session,
    run_id: str,
) -> ResearchAgentRun:
    run = get_run_or_404(db, run_id)
    if run.status in {"completed", "failed"}:
        raise AppError(
            "已完成或失败的研究任务不能取消。",
            code="RESEARCH_AGENT_CANCEL_NOT_ALLOWED",
            status_code=409,
            details={"status": run.status},
        )
    run.cancel_requested = True
    run.status = "cancelled"
    run.completed_at = datetime.utcnow()
    for node in run.nodes:
        if node.status in {"pending", "retry"}:
            node.status = "cancelled"
            node.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(run)
    return run


def get_report(db: Session, run_id: str) -> dict[str, Any]:
    run = get_run_or_404(db, run_id)
    artifact = (
        db.query(ResearchAgentArtifact)
        .filter(
            ResearchAgentArtifact.run_id == run.id,
            ResearchAgentArtifact.artifact_type == "final_report",
        )
        .order_by(ResearchAgentArtifact.created_at.desc())
        .first()
    )
    if artifact is None:
        raise AppError(
            "研究任务尚未生成通过证据校验的最终报告。",
            code="RESEARCH_AGENT_REPORT_NOT_READY",
            status_code=409,
            details={"run_id": run_id, "status": run.status},
        )
    content = _loads(artifact.content_json, {})
    return {
        "run_id": run.id,
        "status": run.status,
        "valid_source_count": artifact.valid_source_count,
        "report": content,
        "created_at": _iso(artifact.created_at),
    }


def serialize_run(db: Session, run: ResearchAgentRun) -> dict[str, Any]:
    nodes = (
        db.query(ResearchAgentNode)
        .filter(ResearchAgentNode.run_id == run.id)
        .order_by(ResearchAgentNode.created_at.asc(), ResearchAgentNode.id.asc())
        .all()
    )
    calls = (
        db.query(ResearchAgentToolCall)
        .filter(ResearchAgentToolCall.run_id == run.id)
        .order_by(ResearchAgentToolCall.started_at.asc())
        .all()
    )
    evidence = (
        db.query(ResearchAgentEvidence)
        .filter(ResearchAgentEvidence.run_id == run.id)
        .order_by(ResearchAgentEvidence.created_at.asc())
        .all()
    )
    artifact = (
        db.query(ResearchAgentArtifact)
        .filter(
            ResearchAgentArtifact.run_id == run.id,
            ResearchAgentArtifact.artifact_type == "final_report",
        )
        .order_by(ResearchAgentArtifact.created_at.desc())
        .first()
    )
    elapsed_seconds = None
    if run.started_at:
        end = run.completed_at or datetime.utcnow()
        elapsed_seconds = max(0, int((end - run.started_at).total_seconds()))
    return {
        "run_id": run.id,
        "goal": run.goal,
        "status": run.status,
        "topic": run.topic,
        "research_question": run.research_question,
        "time_range": run.time_range,
        "analysis_dimensions": _loads(run.analysis_dimensions_json, []),
        "plan": _loads(run.plan_json, None),
        "plan_version": run.plan_version,
        "replan_count": run.replan_count,
        "budget": {
            "max_nodes": run.max_nodes,
            "max_tool_calls": run.max_tool_calls,
            "max_llm_calls": run.max_llm_calls,
            "max_arxiv_requests": run.max_arxiv_requests,
            "max_execution_seconds": run.max_execution_seconds,
            "writer_concurrency": 1,
        },
        "usage": {
            "nodes": len(nodes),
            "tool_calls": run.tool_calls_used,
            "llm_calls": run.llm_calls_used,
            "arxiv_requests": run.arxiv_requests_used,
            "elapsed_seconds": elapsed_seconds,
        },
        "cancel_requested": run.cancel_requested,
        "error": run.error_message,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "nodes": [
            {
                "id": node.id,
                "node_key": node.node_key,
                "node_type": node.node_type,
                "tool_name": node.tool_name,
                "input": _loads(node.input_json, {}),
                "depends_on": _loads(node.dependencies_json, []),
                "reason": node.reason,
                "completion_condition": node.completion_condition,
                "output_summary": _loads(node.output_summary_json, None),
                "status": node.status,
                "retry_count": node.retry_count,
                "retry_limit": node.retry_limit,
                "error": node.error_message,
                "started_at": _iso(node.started_at),
                "completed_at": _iso(node.completed_at),
            }
            for node in nodes
        ],
        "tool_calls": [
            {
                "id": call.id,
                "node_id": call.node_id,
                "tool_name": call.tool_name,
                "attempt": call.attempt,
                "arguments_summary": _loads(
                    call.arguments_summary_json,
                    {},
                ),
                "result_summary": _loads(call.result_summary_json, None),
                "status": call.status,
                "duration_ms": call.duration_ms,
                "error": call.error_message,
                "started_at": _iso(call.started_at),
                "completed_at": _iso(call.completed_at),
            }
            for call in calls
        ],
        "evidence": [
            {
                "id": item.id,
                "node_id": item.node_id,
                "tool_call_id": item.tool_call_id,
                "source_type": item.source_type,
                "paper_id": item.paper_id,
                "page_number": item.page_number,
                "chunk_id": item.chunk_id,
                "arxiv_id": item.arxiv_id,
                "title": item.title,
                "content_hash": item.content_hash,
                "excerpt": item.excerpt,
                "source_url": item.source_url,
                "is_valid": item.is_valid,
                "validation_error": item.validation_error,
            }
            for item in evidence
        ],
        "report_available": artifact is not None,
    }
