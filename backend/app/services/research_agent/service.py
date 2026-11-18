from __future__ import annotations

import json
from datetime import datetime
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
from app.config import get_settings
from app.schemas.research_agent import (
    AgentBudgetRequest,
    PlannerAnalysis,
    PlannerPlan,
    ResearchAgentPlanUpdate,
    ValidatedPlan,
)
from app.services.research_agent.executor import (
    persist_plan_nodes,
)
from app.services.research_agent.planner import (
    PlanValidationError,
    ResearchPlanner,
    validate_plan,
)
from app.services.research_agent.tools import TOOL_REGISTRY, ToolRegistry
from app.services.research_agent.worker import start_agent_run


PLAN_HISTORY_ARTIFACT = "plan_version"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def _build_research_run(
    *,
    goal: str,
    budget: AgentBudgetRequest,
    status: str,
) -> ResearchAgentRun:
    run = ResearchAgentRun(
        goal=goal,
        topic="",
        research_question="",
        analysis_dimensions_json="[]",
        status=status,
        plan_version=0,
        replan_count=0,
        max_nodes=budget.max_nodes,
        max_tool_calls=budget.max_tool_calls,
        max_llm_calls=budget.max_llm_calls,
        max_arxiv_requests=budget.max_arxiv_requests,
        max_replans=budget.max_replans,
        max_execution_seconds=budget.max_execution_seconds,
        tool_calls_used=0,
        llm_calls_used=0,
        arxiv_requests_used=0,
        cancel_requested=False,
    )
    return run


def create_research_run(
    db: Session,
    *,
    goal: str,
    budget: AgentBudgetRequest,
) -> ResearchAgentRun:
    run = _build_research_run(
        goal=goal,
        budget=budget,
        status="pending",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _plan_estimate(
    plan: ValidatedPlan,
    *,
    registry: ToolRegistry = TOOL_REGISTRY,
) -> dict[str, int]:
    llm_calls = 0
    for node in plan.nodes:
        try:
            llm_calls += registry.get(node.tool_name).llm_calls_per_execution
        except Exception:
            continue
    return {
        "node_count": len(plan.nodes),
        "tool_call_estimate": len(plan.nodes),
        "arxiv_request_estimate": sum(
            node.tool_name == "search_arxiv" for node in plan.nodes
        ),
        "llm_call_estimate": llm_calls,
    }


def _record_plan_version(
    db: Session,
    run: ResearchAgentRun,
    plan: ValidatedPlan,
    *,
    action: str,
    change_reason: str,
) -> None:
    db.add(
        ResearchAgentArtifact(
            run_id=run.id,
            artifact_type=PLAN_HISTORY_ARTIFACT,
            content_json=_json_dump(
                {
                    "version": plan.version,
                    "action": action,
                    "change_reason": change_reason,
                    "plan": plan.model_dump(mode="json"),
                }
            ),
            valid_source_count=0,
        )
    )


def _apply_plan_to_run(
    run: ResearchAgentRun,
    plan: ValidatedPlan,
    budget: AgentBudgetRequest,
) -> None:
    run.topic = plan.topic
    run.research_question = plan.research_question
    run.time_range = plan.time_range
    run.analysis_dimensions_json = _json_dump(plan.analysis_dimensions)
    run.plan_json = _json_dump(plan.model_dump(mode="json"))
    run.plan_version = plan.version
    run.max_nodes = budget.max_nodes
    run.max_tool_calls = budget.max_tool_calls
    run.max_llm_calls = budget.max_llm_calls
    run.max_arxiv_requests = budget.max_arxiv_requests
    run.max_replans = budget.max_replans
    run.max_execution_seconds = budget.max_execution_seconds


def create_research_plan(
    db: Session,
    *,
    goal: str,
    budget: AgentBudgetRequest,
    planner: ResearchPlanner | None = None,
) -> ResearchAgentRun:
    planner = planner or ResearchPlanner()
    settings = get_settings()
    try:
        plan = planner.create_plan(goal, budget)
    except PlanValidationError as exc:
        raise AppError(
            f"研究计划生成失败：{exc}",
            code=exc.code,
            status_code=422,
            details=exc.details,
        ) from exc
    run = _build_research_run(
        goal=goal,
        budget=budget,
        status="draft",
    )
    if settings.llm_api_key and budget.max_llm_calls > 0:
        run.llm_calls_used = min(
            budget.max_llm_calls,
            int(getattr(planner, "last_llm_calls_used", 0) or 0),
        )
    db.add(run)
    db.flush()
    planner_diagnostics = getattr(
        planner,
        "last_llm_diagnostics",
        None,
    )
    if planner_diagnostics:
        db.add(
            ResearchAgentArtifact(
                run_id=run.id,
                artifact_type="llm_diagnostics",
                content_json=json.dumps(
                    {
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
                    ensure_ascii=False,
                ),
                valid_source_count=0,
            )
        )
    _apply_plan_to_run(run, plan, budget)
    _record_plan_version(
        db,
        run,
        plan,
        action="generated",
        change_reason="Planner 生成初始计划",
    )
    db.commit()
    db.refresh(run)
    return run


def update_research_plan(
    db: Session,
    plan_id: str,
    request: ResearchAgentPlanUpdate,
    *,
    registry: ToolRegistry = TOOL_REGISTRY,
) -> ResearchAgentRun:
    run = get_run_or_404(db, plan_id)
    if run.status != "draft":
        raise AppError(
            "只有草案计划可以修改。",
            code="RESEARCH_AGENT_PLAN_NOT_EDITABLE",
            status_code=409,
            details={"run_id": run.id, "status": run.status},
        )
    current_payload = _loads(run.plan_json, None)
    if current_payload is None:
        raise AppError(
            "研究计划草案不存在。",
            code="RESEARCH_AGENT_PLAN_NOT_FOUND",
            status_code=409,
            details={"run_id": run.id},
        )
    try:
        current = ValidatedPlan.model_validate(current_payload)
    except Exception as exc:
        raise AppError(
            "研究计划草案无法读取。",
            code="RESEARCH_AGENT_PLAN_CORRUPTED",
            status_code=500,
            details={"run_id": run.id},
        ) from exc

    candidate = PlannerPlan(
        analysis=PlannerAnalysis(
            topic=request.topic,
            research_question=request.research_question,
            time_range=request.time_range,
            analysis_dimensions=request.analysis_dimensions,
        ),
        nodes=request.nodes,
    )
    try:
        validated = validate_plan(
            candidate,
            request.budget,
            version=current.version + 1,
            generated_by=current.generated_by,
            registry=registry,
            edited_by_user=True,
        )
    except PlanValidationError as exc:
        raise AppError(
            f"计划修改未通过校验：{exc}",
            code=exc.code,
            status_code=422,
            details=exc.details,
        ) from exc

    run.goal = request.goal
    _apply_plan_to_run(run, validated, request.budget)
    _record_plan_version(
        db,
        run,
        validated,
        action="edited",
        change_reason=request.change_reason,
    )
    db.commit()
    db.refresh(run)
    return run


def confirm_research_plan(
    db: Session,
    plan_id: str,
    *,
    registry: ToolRegistry = TOOL_REGISTRY,
) -> ResearchAgentRun:
    run = get_run_or_404(db, plan_id)
    if run.status != "draft":
        db.refresh(run)
        return run
    payload = _loads(run.plan_json, None)
    if payload is None:
        raise AppError(
            "研究计划草案不存在。",
            code="RESEARCH_AGENT_PLAN_NOT_FOUND",
            status_code=409,
            details={"run_id": run.id},
        )
    try:
        plan = ValidatedPlan.model_validate(payload)
        budget = AgentBudgetRequest(
            max_nodes=run.max_nodes,
            max_tool_calls=run.max_tool_calls,
            max_llm_calls=run.max_llm_calls,
            max_arxiv_requests=run.max_arxiv_requests,
            max_replans=run.max_replans,
            max_execution_seconds=run.max_execution_seconds,
        )
        validated = validate_plan(
            PlannerPlan(
                analysis=PlannerAnalysis(
                    topic=plan.topic,
                    research_question=plan.research_question,
                    time_range=plan.time_range,
                    analysis_dimensions=plan.analysis_dimensions,
                ),
                nodes=plan.nodes,
            ),
            budget,
            version=plan.version,
            generated_by=plan.generated_by,
            registry=registry,
            edited_by_user=plan.edited_by_user,
        )
    except (PlanValidationError, ValueError) as exc:
        code = getattr(exc, "code", "RESEARCH_AGENT_PLAN_INVALID")
        details = getattr(exc, "details", {})
        raise AppError(
            f"计划确认校验失败：{exc}",
            code=code,
            status_code=422,
            details=details,
        ) from exc

    claimed = (
        db.query(ResearchAgentRun)
        .filter(
            ResearchAgentRun.id == run.id,
            ResearchAgentRun.status == "draft",
        )
        .update(
            {
                ResearchAgentRun.status: "pending",
                ResearchAgentRun.error_message: None,
                ResearchAgentRun.cancel_requested: False,
            },
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.rollback()
        return get_run_or_404(db, plan_id)
    db.expire(run)
    db.refresh(run)
    _apply_plan_to_run(run, validated, budget)
    persist_plan_nodes(db, run, validated)
    db.commit()
    db.refresh(run)
    start_research_run_from_session(db, run.id)
    return run


def serialize_tool_catalog(
    registry: ToolRegistry = TOOL_REGISTRY,
) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "cost_class": spec.cost_class,
            "max_attempts": spec.max_attempts,
            "max_calls_per_run": spec.max_calls_per_run,
            "llm_calls_per_execution": spec.llm_calls_per_execution,
            "input_schema": spec.input_schema.model_json_schema(),
            "output_schema": spec.output_schema.model_json_schema(),
        }
        for spec in registry.specs()
    ]


def start_research_run_thread(
    run_id: str,
    session_factory: Callable[[], Session],
) -> None:
    start_agent_run(run_id, session_factory)


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
    if run.status in {
        "completed",
        "completed_with_warnings",
        "partial",
        "failed",
    }:
        raise AppError(
            "已完成或失败的研究任务不能取消。",
            code="RESEARCH_AGENT_CANCEL_NOT_ALLOWED",
            status_code=409,
            details={"status": run.status},
        )
    now = datetime.utcnow()
    run.cancel_requested = True
    lease_active = bool(
        run.lease_owner
        and run.lease_expires_at
        and run.lease_expires_at > now
        and run.status in {"running", "cancelling"}
    )
    if lease_active:
        run.status = "cancelling"
    else:
        run.status = "cancelled"
        run.completed_at = now
        for node in run.nodes:
            if node.status in {"pending", "retry", "running"}:
                node.status = "cancelled"
                node.completed_at = now
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
    plan_history_rows = (
        db.query(ResearchAgentArtifact)
        .filter(
            ResearchAgentArtifact.run_id == run.id,
            ResearchAgentArtifact.artifact_type == PLAN_HISTORY_ARTIFACT,
        )
        .order_by(ResearchAgentArtifact.created_at.asc())
        .all()
    )
    node_artifact_rows = (
        db.query(ResearchAgentArtifact)
        .filter(
            ResearchAgentArtifact.run_id == run.id,
            ResearchAgentArtifact.artifact_type.in_(
                (
                    "node_input",
                    "node_output",
                    "node_error",
                    "strategy_decision",
                )
            ),
        )
        .order_by(ResearchAgentArtifact.created_at.asc())
        .all()
    )
    node_artifacts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in node_artifact_rows:
        content = _loads(row.content_json, {})
        node_key = content.get("node_key")
        if isinstance(node_key, str):
            node_artifacts[(node_key, row.artifact_type)] = content
    llm_diagnostics = [
        {
            **_loads(row.content_json, {}),
            "created_at": _iso(row.created_at),
        }
        for row in (
            db.query(ResearchAgentArtifact)
            .filter(
                ResearchAgentArtifact.run_id == run.id,
                ResearchAgentArtifact.artifact_type == "llm_diagnostics",
            )
            .order_by(ResearchAgentArtifact.created_at.asc())
            .all()
        )
    ]
    plan_payload = _loads(run.plan_json, None)
    plan_estimate = None
    if plan_payload is not None:
        try:
            plan_estimate = _plan_estimate(
                ValidatedPlan.model_validate(plan_payload)
            )
        except Exception:
            plan_estimate = None
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
        "plan": plan_payload,
        "plan_estimate": plan_estimate,
        "plan_history": [
            {
                "version": _loads(row.content_json, {}).get("version"),
                "action": _loads(row.content_json, {}).get("action"),
                "change_reason": _loads(row.content_json, {}).get("change_reason"),
                "plan": _loads(row.content_json, {}).get("plan"),
                "created_at": _iso(row.created_at),
            }
            for row in plan_history_rows
        ],
        "plan_version": run.plan_version,
        "replan_count": run.replan_count,
        "budget": {
            "max_nodes": run.max_nodes,
            "max_tool_calls": run.max_tool_calls,
            "max_llm_calls": run.max_llm_calls,
            "max_arxiv_requests": run.max_arxiv_requests,
            "max_replans": run.max_replans,
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
                "execution_input": node_artifacts.get(
                    (node.node_key, "node_input")
                ),
                "structured_output": node_artifacts.get(
                    (node.node_key, "node_output")
                ),
                "error_output": node_artifacts.get(
                    (node.node_key, "node_error")
                ),
                "strategy_decision": node_artifacts.get(
                    (node.node_key, "strategy_decision")
                ),
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
        "llm_diagnostics": llm_diagnostics,
        "worker_state": {
            "lease_status": (
                "active"
                if run.lease_owner
                and run.lease_expires_at
                and run.lease_expires_at > datetime.utcnow()
                else "expired"
                if run.lease_owner
                else "released"
            ),
            "lease_generation": run.lease_generation,
            "lease_expires_at": _iso(run.lease_expires_at),
            "heartbeat_at": _iso(run.heartbeat_at),
            "resume_count": run.resume_count,
        },
        "replan_decisions": [
            {
                **_loads(row.content_json, {}),
                "created_at": _iso(row.created_at),
            }
            for row in (
                db.query(ResearchAgentArtifact)
                .filter(
                    ResearchAgentArtifact.run_id == run.id,
                    ResearchAgentArtifact.artifact_type == "replan_decision",
                )
                .order_by(ResearchAgentArtifact.created_at.asc())
                .all()
            )
        ],
        "recoveries": [
            {
                **_loads(row.content_json, {}),
                "created_at": _iso(row.created_at),
            }
            for row in (
                db.query(ResearchAgentArtifact)
                .filter(
                    ResearchAgentArtifact.run_id == run.id,
                    ResearchAgentArtifact.artifact_type == "run_recovery",
                )
                .order_by(ResearchAgentArtifact.created_at.asc())
                .all()
            )
        ],
    }
