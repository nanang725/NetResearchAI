from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.research_agent.tools as tool_module
import app.services.research_agent.planner as planner_module
import app.services.research_agent.service as research_agent_service
import app.api.research_agent as research_agent_api
from app.database import get_db
from app.database import Base
from app.core.exceptions import AppError, app_error_handler
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.models.research_agent import (
    ResearchAgentArtifact,
    ResearchAgentEvidence,
    ResearchAgentNode,
    ResearchAgentRun,
    ResearchAgentToolCall,
)
from app.schemas.research_agent import (
    AgentBudgetRequest,
    InspectLocalLibraryOutput,
    PlannerAnalysis,
    PlannerPlan,
    PlannedNode,
    ResearchAgentPlanUpdate,
    SearchArxivOutput,
    SearchLocalPapersOutput,
    SourceCandidate,
    ValidatedPlan,
)
from app.services.research_agent import executor
from app.services.research_agent.executor import execute_research_run, validate_evidence
from app.services.research_agent.planner import (
    PlanValidationError,
    ResearchPlanner,
    validate_plan,
)
from app.services.research_agent.tools import (
    TOOL_REGISTRY,
    ToolContext,
    ToolExecutionError,
    ToolRegistry,
    ToolSpec,
    classify_tool_error,
)
from app.services.research_agent.worker import claim_run
from app.services import retrieval_service


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "research_agent_test.db"
    engine = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def paper_chunk(session_factory):
    db = session_factory()
    paper = Paper(
        title="A Real Local Paper",
        file_hash="a" * 64,
        original_filename="paper.pdf",
        stored_filename="paper.pdf",
        file_path="paper.pdf",
        file_size=10,
        page_count=1,
        char_count=20,
        parse_status="parsed",
        index_status="indexed",
    )
    db.add(paper)
    db.flush()
    chunk = PaperChunk(
        paper_id=paper.id,
        page_number=2,
        page_chunk_index=0,
        chunk_index=0,
        text="A real local paper reports a verifiable experimental result.",
    )
    db.add(chunk)
    db.commit()
    data = (paper.id, chunk.id)
    db.close()
    return data


def make_run(
    session_factory,
    *,
    status: str = "pending",
) -> ResearchAgentRun:
    db = session_factory()
    run = ResearchAgentRun(
        goal="研究图神经网络流量预测中的方法与证据",
        topic="",
        research_question="",
        status=status,
        plan_version=0,
        replan_count=0,
        max_nodes=8,
        max_tool_calls=12,
        max_llm_calls=4,
        max_arxiv_requests=3,
        max_execution_seconds=180,
        tool_calls_used=0,
        llm_calls_used=0,
        arxiv_requests_used=0,
        cancel_requested=status == "cancelled",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id
    db.close()
    db = session_factory()
    run = db.get(ResearchAgentRun, run_id)
    db.expunge(run)
    db.close()
    return run


def plan_with(
    nodes: list[PlannedNode],
    *,
    version: int = 1,
    generated_by: str = "fallback",
) -> ValidatedPlan:
    return ValidatedPlan(
        version=version,
        topic="图神经网络流量预测",
        research_question="有哪些可核验的方法与结论？",
        time_range=None,
        analysis_dimensions=["方法", "结果"],
        nodes=nodes,
        generated_by=generated_by,
    )


def tool_spec(
    name: str,
    handler,
    *,
    input_schema=None,
    output_schema=None,
    max_attempts: int = 1,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test tool",
        input_schema=input_schema or tool_module.InspectLocalLibraryInput,
        output_schema=output_schema or InspectLocalLibraryOutput,
        side_effect="read_only",
        timeout=5,
        max_attempts=max_attempts,
        max_calls_per_run=3,
        cost_class="local",
        handler=handler,
    )


@dataclass
class StaticPlanner:
    initial: ValidatedPlan
    registry: ToolRegistry

    def create_plan(self, _goal: str, _budget: AgentBudgetRequest) -> ValidatedPlan:
        return self.initial

    def controlled_replan(
        self,
        existing: ValidatedPlan,
        *,
        budget: AgentBudgetRequest,
        source_node_id: str,
        result: dict,
    ) -> ValidatedPlan | None:
        helper = ResearchPlanner(registry=self.registry)
        return helper.controlled_replan(
            existing,
            budget=budget,
            source_node_id=source_node_id,
            result=result,
        )


def test_unregistered_tool_is_rejected():
    plan = PlannerPlan(
        analysis=PlannerAnalysis(
            topic="网络流量预测",
            research_question="有哪些方法？",
        ),
        nodes=[
            PlannedNode(
                id="danger",
                tool_name="os.system",
                arguments={},
                reason="attempt arbitrary code",
                completion_condition="never",
            )
        ],
    )
    with pytest.raises(PlanValidationError) as captured:
        validate_plan(
            plan,
            AgentBudgetRequest(),
            version=1,
        )
    assert captured.value.code == "AGENT_PLAN_UNREGISTERED_TOOL"


def test_invalid_tool_arguments_are_rejected():
    plan = PlannerPlan(
        analysis=PlannerAnalysis(
            topic="网络流量预测",
            research_question="有哪些方法？",
        ),
        nodes=[
            PlannedNode(
                id="search",
                tool_name="search_local_papers",
                arguments={"query": "network traffic", "limit": 999},
                reason="search local evidence",
                completion_condition="return evidence",
            )
        ],
    )
    with pytest.raises(PlanValidationError) as captured:
        validate_plan(plan, AgentBudgetRequest(), version=1)
    assert captured.value.code == "AGENT_PLAN_INVALID_PARAMETERS"


def test_cycle_dependency_is_rejected():
    plan = PlannerPlan(
        analysis=PlannerAnalysis(
            topic="网络流量预测",
            research_question="有哪些方法？",
        ),
        nodes=[
            PlannedNode(
                id="a",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                depends_on=["b"],
                reason="a",
                completion_condition="a",
            ),
            PlannedNode(
                id="b",
                tool_name="get_citation_graph",
                arguments={"limit": 5},
                depends_on=["a"],
                reason="b",
                completion_condition="b",
            ),
        ],
    )
    with pytest.raises(PlanValidationError) as captured:
        validate_plan(plan, AgentBudgetRequest(), version=1)
    assert captured.value.code == "AGENT_PLAN_CYCLE"


def test_over_budget_plan_is_rejected():
    plan = PlannerPlan(
        analysis=PlannerAnalysis(
            topic="网络流量预测",
            research_question="有哪些方法？",
        ),
        nodes=[
            PlannedNode(
                id="inspect",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                reason="inspect",
                completion_condition="inspect",
            ),
            PlannedNode(
                id="graph",
                tool_name="get_citation_graph",
                arguments={"limit": 5},
                reason="graph",
                completion_condition="graph",
            ),
        ],
    )
    with pytest.raises(PlanValidationError) as captured:
        validate_plan(
            plan,
            AgentBudgetRequest(max_nodes=1),
            version=1,
        )
    assert captured.value.code == "AGENT_PLAN_NODE_BUDGET_EXCEEDED"


def test_normal_plan_calls_real_tool_adapter(
    session_factory,
    paper_chunk,
    monkeypatch,
):
    paper_id, chunk_id = paper_chunk

    def fake_hybrid(**kwargs):
        assert kwargs["paper_id"] == paper_id
        return [
            {
                "paper_id": paper_id,
                "page_number": 2,
                "chunk_id": chunk_id,
                "paper_title": "A Real Local Paper",
                "text": "A real local paper reports a verifiable experimental result.",
            }
        ]

    monkeypatch.setattr(tool_module, "search_chunks_hybrid", fake_hybrid)
    db = session_factory()
    try:
        result = TOOL_REGISTRY.execute(
            "search_local_papers",
            {"query": "verifiable result", "limit": 1, "paper_ids": [paper_id]},
            ToolContext(
                db=db,
                run_id="run",
                node_id="node",
                cancel_check=lambda: False,
            ),
        )
    finally:
        db.close()
    assert result["source"] == "local_hybrid_retrieval"
    assert result["result_count"] == 1
    assert result["sources"][0]["chunk_id"] == chunk_id


def test_tool_failure_is_persisted(session_factory, monkeypatch):
    def fail_handler(_context, _validated):
        raise RuntimeError("simulated adapter failure")

    registry = ToolRegistry()
    registry.register(
        tool_spec("failing_tool", fail_handler)
    )
    plan = plan_with(
        [
            PlannedNode(
                id="fail",
                tool_name="failing_tool",
                arguments={},
                reason="test failure state",
                completion_condition="failure recorded",
            )
        ]
    )
    run = make_run(session_factory)
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        stored_run = db.get(ResearchAgentRun, run.id)
        node = db.query(ResearchAgentNode).filter_by(run_id=run.id).one()
        call = db.query(ResearchAgentToolCall).filter_by(run_id=run.id).one()
        assert stored_run.status == "failed"
        assert node.status == "failed"
        assert call.status == "failed"
        assert "simulated adapter failure" in (call.error_message or "")
        assert call.result_summary_json is not None
        assert '"error_category":"internal_error"' in call.result_summary_json
    finally:
        db.close()


def test_local_no_results_triggers_controlled_external_search(
    session_factory,
    monkeypatch,
):
    calls: list[str] = []

    def local_empty(_context, _validated):
        calls.append("local")
        return SearchLocalPapersOutput(
            query="network traffic",
            result_count=0,
            source="local_hybrid_retrieval",
            sources=[],
            result_paper_ids=[],
        )

    def external_one(_context, _validated):
        calls.append("arxiv")
        return SearchArxivOutput(
            query="network traffic",
            result_count=1,
            source="arxiv_official_api",
            total_results=1,
            sources=[
                SourceCandidate(
                    source_type="external_arxiv_abstract",
                    arxiv_id="2401.01234",
                    title="External Abstract Paper",
                    excerpt="This is an external abstract only.",
                    source_url="https://arxiv.org/abs/2401.01234",
                )
            ],
        )

    registry = ToolRegistry()
    registry.register(
        tool_spec(
            "search_local_papers",
            local_empty,
            input_schema=tool_module.SearchLocalPapersInput,
            output_schema=SearchLocalPapersOutput,
        )
    )
    registry.register(
        tool_spec(
            "search_arxiv",
            external_one,
            input_schema=tool_module.SearchArxivInput,
            output_schema=SearchArxivOutput,
        )
    )
    plan = plan_with(
        [
            PlannedNode(
                id="local",
                tool_name="search_local_papers",
                arguments={"query": "network traffic", "limit": 5},
                reason="search local first",
                completion_condition="return local evidence",
            )
        ]
    )
    run = make_run(session_factory)
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        stored_run = db.get(ResearchAgentRun, run.id)
        nodes = db.query(ResearchAgentNode).filter_by(run_id=run.id).all()
        evidence = db.query(ResearchAgentEvidence).filter_by(run_id=run.id).all()
        artifact = (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="final_report")
            .one()
        )
        assert calls == ["local", "arxiv"]
        assert stored_run.replan_count == 1
        assert stored_run.plan_version == 2
        assert any(node.node_key == "dynamic_external_search" for node in nodes)
        assert evidence[0].source_type == "external_arxiv_abstract"
        assert artifact.valid_source_count == 1
        replan_decision = (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="replan_decision")
            .one()
        )
        decision_payload = json.loads(replan_decision.content_json)
        assert decision_payload["accepted"] is True
        assert decision_payload["old_plan_version"] == 1
        assert decision_payload["new_plan_version"] == 2
        assert (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="plan_version")
            .count()
            >= 1
        )
    finally:
        db.close()


def test_arxiv_abstract_cannot_claim_fulltext_page(session_factory):
    db = session_factory()
    try:
        evidence, valid = validate_evidence(
            db,
            SourceCandidate(
                source_type="external_arxiv_abstract",
                page_number=3,
                arxiv_id="2401.01234",
                title="External Abstract Paper",
                excerpt="Abstract only.",
            ),
        )
    finally:
        db.close()
    assert valid is False
    assert evidence.page_number == 3
    assert "不得伪造" in (evidence.validation_error or "")


def test_forged_local_identifiers_are_rejected(session_factory, paper_chunk):
    paper_id, chunk_id = paper_chunk
    db = session_factory()
    try:
        wrong_page, valid_page = validate_evidence(
            db,
            SourceCandidate(
                source_type="local_full_text",
                paper_id=paper_id,
                page_number=99,
                chunk_id=chunk_id,
                excerpt="forged page",
            ),
        )
        wrong_chunk, valid_chunk = validate_evidence(
            db,
            SourceCandidate(
                source_type="local_full_text",
                paper_id=paper_id,
                page_number=2,
                chunk_id=chunk_id + 1000,
                excerpt="forged chunk",
            ),
        )
        wrong_paper, valid_paper = validate_evidence(
            db,
            SourceCandidate(
                source_type="local_full_text",
                paper_id=paper_id + 1000,
                page_number=2,
                chunk_id=chunk_id,
                excerpt="forged paper",
            ),
        )
    finally:
        db.close()
    assert not valid_page and not valid_chunk and not valid_paper
    assert all(
        item.validation_error for item in (wrong_page, wrong_chunk, wrong_paper)
    )


def test_cancelled_run_does_not_start_new_nodes(session_factory):
    registry = ToolRegistry()

    def should_not_run(_context, _validated):
        raise AssertionError("cancelled node must not execute")

    registry.register(tool_spec("never_run", should_not_run))
    plan = plan_with(
        [
            PlannedNode(
                id="never",
                tool_name="never_run",
                arguments={},
                reason="must remain cancelled",
                completion_condition="never",
            )
        ]
    )
    run = make_run(session_factory, status="cancelled")
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        assert db.query(ResearchAgentToolCall).count() == 0
        assert db.get(ResearchAgentRun, run.id).status == "cancelled"
    finally:
        db.close()


def test_report_without_valid_sources_cannot_complete(
    session_factory,
    monkeypatch,
):
    def no_evidence(_context, _validated):
        return InspectLocalLibraryOutput(
            paper_count=0,
            result_count=0,
            papers=[],
        )

    registry = ToolRegistry()
    registry.register(tool_spec("empty_library", no_evidence))
    plan = plan_with(
        [
            PlannedNode(
                id="empty",
                tool_name="empty_library",
                arguments={},
                reason="produce no evidence",
                completion_condition="record empty result",
            )
        ]
    )
    run = make_run(session_factory)
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        stored_run = db.get(ResearchAgentRun, run.id)
        assert stored_run.status == "failed"
        assert "有效来源" in (stored_run.error_message or "")
        assert (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="final_report")
            .count()
            == 0
        )
    finally:
        db.close()


def test_research_agent_api_create_read_cancel_and_report(
    session_factory,
    monkeypatch,
):
    monkeypatch.setattr(
        research_agent_api,
        "start_research_run_from_session",
        lambda _db, _run_id: None,
    )
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(research_agent_api.router, prefix="/api")

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        created = client.post(
            "/api/research-agent/runs",
            json={
                "goal": "研究边缘网络中的可验证流量预测方法",
                "budget": {
                    "max_nodes": 8,
                    "max_tool_calls": 12,
                    "max_llm_calls": 4,
                    "max_arxiv_requests": 3,
                    "max_execution_seconds": 180,
                },
            },
        )
        assert created.status_code == 202
        payload = created.json()["data"]
        run_id = payload["run_id"]
        assert payload["status"] == "pending"

        loaded = client.get(f"/api/research-agent/runs/{run_id}")
        assert loaded.status_code == 200
        assert loaded.json()["data"]["run_id"] == run_id

        report = client.get(f"/api/research-agent/runs/{run_id}/report")
        assert report.status_code == 409
        assert report.json()["error"]["code"] == "RESEARCH_AGENT_REPORT_NOT_READY"

        cancelled = client.post(f"/api/research-agent/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"


def test_legacy_auto_start_api_counts_planner_repair_calls(
    session_factory,
    monkeypatch,
):
    plan_payload = {
        "analysis": {
            "topic": "局部论文库检查",
            "research_question": "当前本地论文库包含哪些论文？",
            "time_range": None,
            "analysis_dimensions": ["论文数量"],
        },
        "nodes": [
            {
                "id": "inspect",
                "tool_name": "inspect_local_library",
                "arguments": {"query": "", "limit": 10},
                "depends_on": [],
                "reason": "读取真实论文库。",
                "completion_condition": "返回论文状态。",
                "retry_limit": 0,
            }
        ],
    }
    contents = ['{"analysis":"invalid"', json.dumps(plan_payload)]

    class PlannerRepairLLM:
        def __init__(self):
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create),
            )

        def create(self, **kwargs):
            self.calls.append(kwargs)
            index = min(len(self.calls) - 1, len(contents) - 1)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=contents[index]),
                        finish_reason="stop",
                    )
                ]
            )

    llm = PlannerRepairLLM()
    monkeypatch.setattr(
        planner_module,
        "get_llm_client",
        lambda: llm,
    )

    def start_synchronously(_db, run_id):
        execute_research_run(run_id, session_factory)

    monkeypatch.setattr(
        research_agent_api,
        "start_research_run_from_session",
        start_synchronously,
    )
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(research_agent_api.router, prefix="/api")

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        created = client.post(
            "/api/research-agent/runs",
            json={
                "goal": "检查本地论文库中的论文数量与方法证据",
                "budget": {
                    "max_nodes": 2,
                    "max_tool_calls": 2,
                    "max_llm_calls": 4,
                    "max_arxiv_requests": 0,
                    "max_replans": 0,
                    "max_execution_seconds": 60,
                },
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["run_id"]
        loaded = client.get(f"/api/research-agent/runs/{run_id}").json()["data"]

    assert len(llm.calls) == 2
    assert loaded["usage"]["llm_calls"] == 2
    assert loaded["usage"]["llm_calls"] <= loaded["budget"]["max_llm_calls"]
    assert loaded["plan"]["generated_by"] == "llm"
    assert len(loaded["llm_diagnostics"]) == 1


def test_research_agent_openapi_exposes_run_and_report_contracts():
    app = FastAPI()
    app.include_router(research_agent_api.router, prefix="/api")
    schema = app.openapi()
    components = schema["components"]["schemas"]

    run_properties = components["ResearchAgentRunData"]["properties"]
    report_properties = components[
        "ResearchAgentReportPayload"
    ]["properties"]
    assert "llm_diagnostics" in run_properties
    assert "worker_state" in run_properties
    assert "generation_mode" in report_properties

    run_response = schema["paths"][
        "/api/research-agent/runs/{run_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    report_response = schema["paths"][
        "/api/research-agent/runs/{run_id}/report"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    tools_response = schema["paths"][
        "/api/research-agent/tools"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "ResearchAgentRunEnvelope" in str(run_response)
    assert "ResearchAgentReportEnvelope" in str(report_response)
    assert "ResearchAgentToolCatalogEnvelope" in str(tools_response)


def test_llm_planner_success_returns_structured_plan(monkeypatch):
    payload = {
        "analysis": {
            "topic": "图神经网络流量预测",
            "research_question": "不同方法有哪些可核验差异？",
            "time_range": "2020-2025",
            "analysis_dimensions": ["方法", "结果", "局限"],
        },
        "nodes": [
            {
                "id": "inspect",
                "tool_name": "inspect_local_library",
                "arguments": {"query": "流量", "limit": 10},
                "depends_on": [],
                "reason": "确认可用本地论文。",
                "completion_condition": "返回论文状态。",
                "retry_limit": 0,
            },
            {
                "id": "search",
                "tool_name": "search_local_papers",
                "arguments": {"query": "流量预测", "limit": 5},
                "depends_on": ["inspect"],
                "reason": "查找真实全文证据。",
                "completion_condition": "返回带页码的 Chunk。",
                "retry_limit": 1,
            },
            {
                "id": "compare",
                "tool_name": "compare_papers",
                "arguments": {
                    "topic": "图神经网络流量预测方法比较",
                    "include_future_work": True,
                    "paper_ids": [999, 1000],
                },
                "depends_on": [],
                "input_bindings": {
                    "paper_ids": {
                        "source_node_id": "search",
                        "output_key": "result_paper_ids",
                        "max_items": 5,
                    }
                },
                "reason": "比较检索命中的真实论文。",
                "completion_condition": "返回可核验比较结果。",
                "retry_limit": 0,
            },
        ],
    }
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=__import__("json").dumps(payload, ensure_ascii=False)
                )
            )
        ]
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response)
        )
    )
    monkeypatch.setattr(planner_module, "get_llm_client", lambda: fake_client)

    plan = ResearchPlanner().create_plan(
        "比较图神经网络流量预测的方法与证据",
        AgentBudgetRequest(),
    )

    assert plan.generated_by == "llm"
    assert [node.id for node in plan.nodes] == ["inspect", "search", "compare"]
    compare = next(node for node in plan.nodes if node.id == "compare")
    assert "search" in compare.depends_on
    assert "paper_ids" not in compare.arguments


def test_fallback_plan_is_explicit_when_llm_is_unavailable(monkeypatch):
    def unavailable():
        raise RuntimeError("planner unavailable")

    monkeypatch.setattr(planner_module, "get_llm_client", unavailable)
    plan = ResearchPlanner().create_plan(
        "梳理网络流量预测研究中的方法与证据",
        AgentBudgetRequest(),
    )

    assert plan.generated_by == "fallback"
    assert len(plan.nodes) >= 1
    assert all(node.tool_name in TOOL_REGISTRY.names() for node in plan.nodes)


def test_planner_json_repair_is_bounded(monkeypatch):
    valid = {
        "analysis": {
            "topic": "network evidence",
            "research_question": "Which local evidence is available?",
            "time_range": None,
            "analysis_dimensions": ["method"],
        },
        "nodes": [
            {
                "id": "inspect",
                "tool_name": "inspect_local_library",
                "arguments": {"query": "", "limit": 10},
                "depends_on": [],
                "reason": "inspect",
                "completion_condition": "inventory returned",
                "retry_limit": 0,
            }
        ],
    }
    contents = ['{"analysis": "truncated"', json.dumps(valid)]

    class SequencePlannerLLM:
        def __init__(self):
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create),
            )

        def create(self, **kwargs):
            self.calls.append(kwargs)
            index = min(len(self.calls) - 1, len(contents) - 1)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=contents[index]),
                        finish_reason="stop",
                    )
                ]
            )

    llm = SequencePlannerLLM()
    monkeypatch.setattr(
        planner_module,
        "get_llm_client",
        lambda: llm,
    )

    planner = ResearchPlanner()
    plan = planner.create_plan(
        "Inspect local evidence",
        AgentBudgetRequest(),
    )

    assert plan.generated_by == "llm"
    assert [node.id for node in plan.nodes] == ["inspect"]
    assert planner.last_llm_calls_used == 2
    assert planner.last_llm_diagnostics["repaired"] is True
    assert len(llm.calls) == 2
    assert "Repair the supplied JSON only" in (
        llm.calls[1]["messages"][0]["content"]
    )


def test_zero_llm_budget_skips_planner_model_call(monkeypatch):
    llm_factory = Mock(
        side_effect=AssertionError("LLM must not be called with zero budget")
    )
    monkeypatch.setattr(planner_module, "get_llm_client", llm_factory)

    planner = ResearchPlanner()
    plan = planner.create_plan(
        "Inspect local evidence without an LLM",
        AgentBudgetRequest(
            max_nodes=2,
            max_tool_calls=2,
            max_llm_calls=0,
            max_arxiv_requests=0,
            max_replans=0,
            max_execution_seconds=30,
        ),
    )

    assert plan.generated_by == "fallback"
    assert planner.last_llm_calls_used == 0
    llm_factory.assert_not_called()


def test_dynamic_llm_consumer_preserves_report_reserve_and_limit(
    session_factory,
):
    run = make_run(session_factory)
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        stored.llm_calls_used = 1
        stored.max_llm_calls = 4
        db.commit()
        consume = executor._dynamic_llm_call_consumer(db, stored)

        consume()
        consume()
        assert stored.llm_calls_used == 3
        with pytest.raises(ToolExecutionError) as captured:
            consume()

        assert captured.value.code == "AGENT_REPORT_LLM_RESERVE"
        db.refresh(stored)
        assert stored.llm_calls_used == 3
        assert stored.llm_calls_used < stored.max_llm_calls
    finally:
        db.close()


def test_plan_draft_creation_does_not_persist_nodes_or_start_execution(
    session_factory,
    monkeypatch,
):
    plan = plan_with(
        [
            PlannedNode(
                id="inspect",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                reason="inspect",
                completion_condition="inspect",
            )
        ]
    )
    start_calls: list[str] = []
    monkeypatch.setattr(
        research_agent_service,
        "start_research_run_from_session",
        lambda _db, run_id: start_calls.append(run_id),
    )

    db = session_factory()
    try:
        run = research_agent_service.create_research_plan(
            db,
            goal="分析网络流量预测的方法",
            budget=AgentBudgetRequest(),
            planner=StaticPlanner(plan, TOOL_REGISTRY),
        )
        assert run.status == "draft"
        assert run.plan_version == 1
        assert db.query(ResearchAgentNode).filter_by(run_id=run.id).count() == 0
        assert (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="plan_version")
            .count()
            == 1
        )
    finally:
        db.close()
    assert start_calls == []


def test_plan_edit_is_revalidated_and_creates_version_history(session_factory):
    initial = plan_with(
        [
            PlannedNode(
                id="inspect",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                reason="inspect",
                completion_condition="inspect",
            )
        ]
    )
    db = session_factory()
    try:
        run = research_agent_service.create_research_plan(
            db,
            goal="分析网络流量预测的方法",
            budget=AgentBudgetRequest(),
            planner=StaticPlanner(initial, TOOL_REGISTRY),
        )
        invalid = ResearchAgentPlanUpdate(
            goal=run.goal,
            topic=initial.topic,
            research_question=initial.research_question,
            analysis_dimensions=initial.analysis_dimensions,
            nodes=[
                PlannedNode(
                    id="danger",
                    tool_name="os.system",
                    arguments={},
                    reason="invalid",
                    completion_condition="invalid",
                )
            ],
            budget=AgentBudgetRequest(),
        )
        with pytest.raises(AppError) as captured:
            research_agent_service.update_research_plan(db, run.id, invalid)
        assert captured.value.code == "AGENT_PLAN_UNREGISTERED_TOOL"
        db.refresh(run)
        assert run.plan_version == 1

        valid = ResearchAgentPlanUpdate(
            goal="分析网络流量预测的方法与证据",
            topic=initial.topic,
            research_question=initial.research_question,
            analysis_dimensions=initial.analysis_dimensions,
            nodes=[
                PlannedNode(
                    id="inspect",
                    tool_name="inspect_local_library",
                    arguments={"query": "流量", "limit": 10},
                    reason="确认可用本地论文。",
                    completion_condition="返回论文状态。",
                    output_definition={
                        "description": "本地论文元数据",
                        "fields": ["paper_count", "papers"],
                    },
                )
            ],
            budget=AgentBudgetRequest(max_nodes=6),
            change_reason="缩小检索范围",
        )
        updated = research_agent_service.update_research_plan(db, run.id, valid)
        assert updated.plan_version == 2
        assert updated.max_nodes == 6
        assert updated.plan_json is not None
        validated = ValidatedPlan.model_validate_json(updated.plan_json)
        assert validated.edited_by_user is True
        assert (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="plan_version")
            .count()
            == 2
        )
    finally:
        db.close()


def test_plan_confirmation_is_idempotent_and_starts_execution_once(
    session_factory,
    monkeypatch,
):
    plan = plan_with(
        [
            PlannedNode(
                id="inspect",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                reason="inspect",
                completion_condition="inspect",
            )
        ]
    )
    db = session_factory()
    try:
        draft = research_agent_service.create_research_plan(
            db,
            goal="分析网络流量预测的方法",
            budget=AgentBudgetRequest(),
            planner=StaticPlanner(plan, TOOL_REGISTRY),
        )
    finally:
        db.close()

    start_calls: list[str] = []
    monkeypatch.setattr(
        research_agent_service,
        "start_research_run_from_session",
        lambda _db, run_id: start_calls.append(run_id),
    )
    db = session_factory()
    try:
        first = research_agent_service.confirm_research_plan(db, draft.id)
        second = research_agent_service.confirm_research_plan(db, draft.id)
        assert first.id == second.id == draft.id
        assert second.status == "pending"
        assert (
            db.query(ResearchAgentRun)
            .filter_by(id=draft.id)
            .count()
            == 1
        )
        assert db.query(ResearchAgentNode).filter_by(run_id=draft.id).count() == 1
    finally:
        db.close()
    assert start_calls == [draft.id]


def test_plan_api_preview_edit_and_confirm_without_breaking_auto_run(
    session_factory,
    monkeypatch,
):
    plan = plan_with(
        [
            PlannedNode(
                id="inspect",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                reason="inspect",
                completion_condition="inspect",
            )
        ]
    )
    start_calls: list[str] = []
    monkeypatch.setattr(
        research_agent_service,
        "ResearchPlanner",
        lambda: StaticPlanner(plan, TOOL_REGISTRY),
    )
    monkeypatch.setattr(
        research_agent_service,
        "start_research_run_from_session",
        lambda _db, run_id: start_calls.append(run_id),
    )
    monkeypatch.setattr(
        research_agent_api,
        "start_research_run_from_session",
        lambda _db, run_id: start_calls.append(run_id),
    )
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(research_agent_api.router, prefix="/api")

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    budget = {
        "max_nodes": 8,
        "max_tool_calls": 12,
        "max_llm_calls": 4,
        "max_arxiv_requests": 3,
        "max_execution_seconds": 180,
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/research-agent/plans",
            json={"goal": "分析边缘网络流量预测方法", "budget": budget},
        )
        assert created.status_code == 201
        draft = created.json()["data"]
        assert draft["status"] == "draft"
        assert draft["plan"]["generated_by"] == "fallback"
        assert draft["plan_estimate"]["tool_call_estimate"] == 1
        assert start_calls == []

        updated = client.put(
            f"/api/research-agent/plans/{draft['run_id']}",
            json={
                "goal": "分析边缘网络流量预测方法与证据",
                "topic": plan.topic,
                "research_question": plan.research_question,
                "analysis_dimensions": plan.analysis_dimensions,
                "nodes": [
                    {
                        "id": "inspect",
                        "tool_name": "inspect_local_library",
                        "arguments": {"limit": 8},
                        "depends_on": [],
                        "reason": "查看真实馆藏范围。",
                        "completion_condition": "返回论文状态。",
                        "retry_limit": 0,
                    }
                ],
                "budget": {**budget, "max_nodes": 6},
                "change_reason": "调整检索预算",
            },
        )
        assert updated.status_code == 200
        edited = updated.json()["data"]
        assert edited["plan_version"] == 2
        assert edited["plan"]["edited_by_user"] is True
        assert len(edited["plan_history"]) == 2

        first = client.post(
            f"/api/research-agent/plans/{draft['run_id']}/confirm"
        )
        second = client.post(
            f"/api/research-agent/plans/{draft['run_id']}/confirm"
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["data"]["run_id"] == draft["run_id"]
        assert second.json()["data"]["status"] == "pending"

        legacy = client.post(
            "/api/research-agent/runs",
            json={"goal": "研究边缘网络中的流量预测方法", "budget": budget},
        )
        assert legacy.status_code == 202
        assert legacy.json()["data"]["status"] == "pending"

        tools = client.get("/api/research-agent/tools")
        assert tools.status_code == 200
        assert any(
            item["name"] == "search_local_papers"
            for item in tools.json()["data"]
        )
    assert start_calls == [draft["run_id"], legacy.json()["data"]["run_id"]]


def _add_agent_paper(
    db,
    *,
    title: str,
    text: str,
    page_number: int = 1,
) -> tuple[Paper, PaperChunk]:
    paper = Paper(
        title=title,
        file_hash=__import__("hashlib").sha256(
            title.encode("utf-8")
        ).hexdigest(),
        original_filename=f"{title}.pdf",
        stored_filename=f"{title}.pdf",
        file_path=f"{title}.pdf",
        file_size=len(text),
        page_count=1,
        char_count=len(text),
        parse_status="parsed",
        index_status="indexed",
    )
    db.add(paper)
    db.flush()
    chunk = PaperChunk(
        paper_id=paper.id,
        page_number=page_number,
        page_chunk_index=0,
        chunk_index=0,
        text=text,
    )
    db.add(chunk)
    db.commit()
    db.refresh(paper)
    db.refresh(chunk)
    return paper, chunk


def test_agent_full_library_search_finds_paper_after_first_ten(
    session_factory,
    monkeypatch,
):
    db = session_factory()
    created: list[tuple[Paper, PaperChunk]] = []
    try:
        for index in range(10):
            created.append(
                _add_agent_paper(
                    db,
                    title=f"Background Paper {index}",
                    text=(
                        "This background paper studies ordinary network "
                        "congestion and routing observability in detail."
                    ),
                )
            )
        target = _add_agent_paper(
            db,
            title="Target Paper Eleven",
            text=(
                "The uniquexylophone token identifies a verifiable target "
                "study that appears only in the eleventh paper."
            ),
        )
        monkeypatch.setattr(
            retrieval_service,
            "search_similar_chunks",
            Mock(side_effect=RuntimeError("forced BM25-only regression")),
        )
        result = TOOL_REGISTRY.execute(
            "search_local_papers",
            {"query": "uniquexylophone", "limit": 5, "paper_ids": []},
            ToolContext(
                db=db,
                run_id="run",
                node_id="node",
                cancel_check=lambda: False,
            ),
        )
        assert result["status"] == "success"
        assert result["result_paper_ids"] == [target[0].id]
        assert result["sources"][0]["chunk_id"] == target[1].id

        scoped = TOOL_REGISTRY.execute(
            "search_local_papers",
            {
                "query": "uniquexylophone",
                "limit": 5,
                "paper_ids": [created[0][0].id],
            },
            ToolContext(
                db=db,
                run_id="run",
                node_id="node",
                cancel_check=lambda: False,
            ),
        )
        assert scoped["status"] == "empty_result"
        assert scoped["result_paper_ids"] == []
        assert scoped["sources"] == []
    finally:
        db.close()


def test_standard_tool_output_distinguishes_empty_and_validation_error(
    session_factory,
):
    db = session_factory()
    try:
        empty = TOOL_REGISTRY.execute(
            "inspect_local_library",
            {"limit": 5},
            ToolContext(
                db=db,
                run_id="run",
                node_id="node",
                cancel_check=lambda: False,
            ),
        )
        assert empty["success"] is True
        assert empty["tool_name"] == "inspect_local_library"
        assert empty["status"] == "empty_result"
        assert empty["data"]["result_count"] == 0
        assert empty["error"] is None

        with pytest.raises(ToolExecutionError) as captured:
            TOOL_REGISTRY.execute(
                "inspect_local_library",
                {"limit": 999},
                ToolContext(
                    db=db,
                    run_id="run",
                    node_id="node",
                    cancel_check=lambda: False,
                ),
            )
        assert captured.value.category == "validation_error"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ARXIV_TIMEOUT", "timeout"),
        ("ARXIV_RATE_LIMITED", "rate_limited"),
        ("ARXIV_UPSTREAM_ERROR", "external_service_error"),
        ("LLM_INVALID_RESPONSE", "external_service_error"),
        ("REVIEW_SYNTHESIS_FAILED", "external_service_error"),
        ("SOMETHING_NOT_FOUND", "not_found"),
        ("UNEXPECTED_FAILURE", "internal_error"),
    ],
)
def test_tool_error_categories_are_standardized(code, expected):
    assert classify_tool_error(code) == expected


def test_get_paper_structure_returns_only_persisted_structure(
    session_factory,
):
    db = session_factory()
    try:
        paper, chunk = _add_agent_paper(
            db,
            title="Structured Network Paper",
            text="Persisted structured evidence for one page.",
        )
        page = PaperPage(
            paper_id=paper.id,
            page_number=1,
            text=chunk.text,
            char_count=len(chunk.text),
            parse_status="parsed",
            checkpoint_status="completed",
        )
        db.add(page)
        db.flush()
        element = PaperElement(
            paper_id=paper.id,
            page_id=page.id,
            page_number=1,
            element_index=0,
            element_type="paragraph",
            text=chunk.text,
            data="{}",
            bbox="[1.0, 2.0, 3.0, 4.0]",
            section_path="Results",
            extraction_status="extracted",
        )
        db.add(element)
        db.flush()
        chunk.element_id = element.id
        db.commit()

        result = TOOL_REGISTRY.execute(
            "get_paper_structure",
            {
                "paper_id": paper.id,
                "max_pages": 5,
                "max_elements_per_page": 10,
                "include_text": True,
            },
            ToolContext(
                db=db,
                run_id="run",
                node_id="node",
                cancel_check=lambda: False,
            ),
        )
        assert result["status"] == "success"
        assert result["paper_id"] == paper.id
        assert result["pages"][0]["page_number"] == 1
        assert result["pages"][0]["elements"][0]["element_id"] == element.id
        assert result["pages"][0]["elements"][0]["chunk_ids"] == [chunk.id]
        assert result["sections"][0]["heading"] == "Results"

        with pytest.raises(ToolExecutionError) as captured:
            TOOL_REGISTRY.execute(
                "get_paper_structure",
                {"paper_id": 999999},
                ToolContext(
                    db=db,
                    run_id="run",
                    node_id="node",
                    cancel_check=lambda: False,
                ),
            )
        assert captured.value.category == "not_found"
    finally:
        db.close()


def test_bound_compare_and_outline_chain_reads_persisted_outputs(
    session_factory,
    monkeypatch,
):
    db = session_factory()
    try:
        first, first_chunk = _add_agent_paper(
            db,
            title="First Bound Paper",
            text="The first paper reports a verifiable comparison result.",
        )
        second, second_chunk = _add_agent_paper(
            db,
            title="Second Bound Paper",
            text="The second paper reports a complementary comparison result.",
        )
        first_id = first.id
        first_title = first.title
        first_page = first_chunk.page_number
        first_chunk_id = first_chunk.id
        first_text = first_chunk.text
        second_id = second.id
        second_title = second.title
        second_page = second_chunk.page_number
        second_chunk_id = second_chunk.id
        second_text = second_chunk.text
    finally:
        db.close()

    search_results = [
        {
            "paper_id": first_id,
            "page_number": first_page,
            "chunk_id": first_chunk_id,
            "paper_title": first_title,
            "text": first_text,
        },
        {
            "paper_id": second_id,
            "page_number": second_page,
            "chunk_id": second_chunk_id,
            "paper_title": second_title,
            "text": second_text,
        },
    ]
    compare_calls: list[list[int]] = []
    outline_calls: list[int] = []

    monkeypatch.setattr(
        tool_module,
        "search_chunks_hybrid",
        lambda **_kwargs: search_results,
    )

    def fake_review(
        _db,
        *,
        paper_ids,
        topic,
        include_future_work,
        consume_llm_call=None,
    ):
        compare_calls.append(paper_ids)
        return {
            "title": topic,
            "sections": [
                {
                    "heading": "Comparison",
                    "content": "Two persisted papers are compared.",
                }
            ],
            "dimension_comparison": [],
            "paper_coverage": [],
            "sources": [
                {
                    "paper_id": first_id,
                    "page_number": first_page,
                    "chunk_id": first_chunk_id,
                    "title": first_title,
                    "evidence": first_text,
                },
                {
                    "paper_id": second_id,
                    "page_number": second_page,
                    "chunk_id": second_chunk_id,
                    "title": second_title,
                    "evidence": second_text,
                },
            ],
        }

    def fake_outline(_db, *, paper_id, topic):
        outline_calls.append(paper_id)
        return {
            "title": topic,
            "sections": [{"heading": "Evidence", "writing_points": []}],
            "evidence_sources": [
                {
                    "paper_id": first_id,
                    "page_number": first_page,
                    "chunk_id": first_chunk_id,
                    "title": first_title,
                    "excerpt": first_text,
                }
            ],
        }

    monkeypatch.setattr(
        tool_module,
        "generate_literature_review",
        fake_review,
    )
    monkeypatch.setattr(
        tool_module,
        "generate_academic_outline",
        fake_outline,
    )
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    plan = plan_with(
        [
            PlannedNode(
                id="search",
                tool_name="search_local_papers",
                arguments={"query": "comparison", "limit": 5},
                reason="search local",
                completion_condition="return local IDs",
            ),
            PlannedNode(
                id="compare",
                tool_name="compare_papers",
                arguments={
                    "topic": "comparison",
                    "include_future_work": True,
                },
                depends_on=["search"],
                reason="compare bound papers",
                completion_condition="compare real local papers",
                input_bindings={
                    "paper_ids": {
                        "source_node_id": "search",
                        "output_key": "result_paper_ids",
                        "max_items": 5,
                    }
                },
            ),
            PlannedNode(
                id="outline",
                tool_name="generate_research_outline",
                arguments={"topic": "comparison"},
                depends_on=["compare"],
                reason="outline first compared paper",
                completion_condition="return evidence outline",
                input_bindings={
                    "paper_id": {
                        "source_node_id": "compare",
                        "output_key": "selected_paper_ids.0",
                    }
                },
            ),
        ]
    )
    run = make_run(session_factory)
    execute_research_run(
        run.id,
        session_factory,
        planner=StaticPlanner(plan, TOOL_REGISTRY),
    )

    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        assert stored.status == "completed"
        assert compare_calls == [[first_id, second_id]]
        assert outline_calls == [first_id]
        input_artifacts = (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="node_input")
            .all()
        )
        compare_input = next(
            json.loads(row.content_json)
            for row in input_artifacts
            if json.loads(row.content_json).get("node_key") == "compare"
        )
        assert compare_input["resolved_arguments"]["paper_ids"] == [
            first_id,
            second_id,
        ]
        assert compare_input["binding_sources"][0]["source_node_id"] == "search"
        assert (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="node_output")
            .count()
            == 3
        )
        serialized = research_agent_service.serialize_run(db, stored)
        compare_node = next(
            node
            for node in serialized["nodes"]
            if node["node_key"] == "compare"
        )
        assert (
            compare_node["execution_input"]["resolved_arguments"]["paper_ids"]
            == [first_id, second_id]
        )
        assert (
            compare_node["structured_output"]["output"]["status"]
            == "success"
        )
        assert compare_node["structured_output"]["output"]["success"] is True
    finally:
        db.close()


def test_binding_failure_does_not_call_tool_and_persists_error(
    session_factory,
    monkeypatch,
):
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    plan = plan_with(
        [
            PlannedNode(
                id="inspect",
                tool_name="inspect_local_library",
                arguments={"limit": 5},
                reason="inspect",
                completion_condition="return library",
            ),
            PlannedNode(
                id="structure",
                tool_name="get_paper_structure",
                arguments={},
                depends_on=["inspect"],
                reason="read structure",
                completion_condition="return structure",
                input_bindings={
                    "paper_id": {
                        "source_node_id": "inspect",
                        "output_key": "missing_paper_id",
                    }
                },
            ),
        ]
    )
    run = make_run(session_factory)
    execute_research_run(
        run.id,
        session_factory,
        planner=StaticPlanner(plan, TOOL_REGISTRY),
    )
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        assert stored.status == "failed"
        calls = db.query(ResearchAgentToolCall).filter_by(run_id=run.id).all()
        assert len(calls) == 1
        assert calls[0].tool_name == "inspect_local_library"
        error_artifact = (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="node_error")
            .one()
        )
        error_payload = json.loads(error_artifact.content_json)
        assert error_payload["error"]["category"] == "validation_error"
        assert error_payload["error"]["code"] == "AGENT_BINDING_FIELD_NOT_FOUND"
    finally:
        db.close()


def test_empty_search_binding_prevents_compare_execution(
    session_factory,
    monkeypatch,
):
    monkeypatch.setattr(
        tool_module,
        "search_chunks_hybrid",
        lambda **_kwargs: [],
    )
    compare_handler = Mock(side_effect=AssertionError("compare must not run"))
    monkeypatch.setattr(
        tool_module,
        "generate_literature_review",
        compare_handler,
    )
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    plan = plan_with(
        [
            PlannedNode(
                id="search",
                tool_name="search_local_papers",
                arguments={"query": "missing evidence", "limit": 5},
                reason="search",
                completion_condition="return results",
            ),
            PlannedNode(
                id="compare",
                tool_name="compare_papers",
                arguments={"topic": "missing evidence comparison"},
                depends_on=["search"],
                reason="compare",
                completion_condition="compare results",
                input_bindings={
                    "paper_ids": {
                        "source_node_id": "search",
                        "output_key": "result_paper_ids",
                        "max_items": 5,
                    }
                },
            ),
        ]
    )
    run = make_run(session_factory)
    execute_research_run(
        run.id,
        session_factory,
        planner=StaticPlanner(plan, TOOL_REGISTRY),
    )
    compare_handler.assert_not_called()
    db = session_factory()
    try:
        calls = db.query(ResearchAgentToolCall).filter_by(run_id=run.id).all()
        assert [call.tool_name for call in calls] == ["search_local_papers"]
        error_artifact = (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="node_error")
            .one()
        )
        error_payload = json.loads(error_artifact.content_json)
        assert (
            error_payload["error"]["code"]
            == "AGENT_BINDING_TARGET_SCHEMA_INVALID"
        )
    finally:
        db.close()


def test_critical_node_cannot_use_skip_strategy():
    with pytest.raises(ValidationError):
        PlannedNode(
            id="critical",
            tool_name="inspect_local_library",
            arguments={},
            reason="critical",
            completion_condition="critical",
            failure_strategy="skip",
            critical=True,
        )


def test_fallback_tool_must_be_registered_and_read_only():
    plan = PlannerPlan(
        analysis=PlannerAnalysis(
            topic="fallback validation",
            research_question="can fallback validate?",
        ),
        nodes=[
            PlannedNode(
                id="fallback",
                tool_name="inspect_local_library",
                arguments={},
                reason="fallback",
                completion_condition="fallback",
                failure_strategy="fallback_tool",
                fallback_tool="unregistered_tool",
                fallback_arguments={},
            )
        ],
    )
    with pytest.raises(PlanValidationError):
        validate_plan(plan, AgentBudgetRequest(), version=1)


def test_fallback_tool_counts_against_worst_case_tool_budget():
    plan = PlannerPlan(
        analysis=PlannerAnalysis(
            topic="fallback budget",
            research_question="does fallback fit budget?",
        ),
        nodes=[
            PlannedNode(
                id="fallback",
                tool_name="inspect_local_library",
                arguments={},
                reason="fallback",
                completion_condition="fallback",
                failure_strategy="fallback_tool",
                fallback_tool="get_citation_graph",
                fallback_arguments={"limit": 5},
            )
        ],
    )
    with pytest.raises(PlanValidationError) as captured:
        validate_plan(
            plan,
            AgentBudgetRequest(max_tool_calls=1),
            version=1,
        )
    assert captured.value.code == "AGENT_PLAN_CALL_BUDGET_EXCEEDED"
    validated = validate_plan(
        plan,
        AgentBudgetRequest(max_tool_calls=2),
        version=1,
    )
    assert validated.nodes[0].failure_strategy == "fallback_tool"


def test_noncritical_node_can_be_skipped_and_run_reports_warnings(
    session_factory,
    paper_chunk,
    monkeypatch,
):
    paper_id, chunk_id = paper_chunk

    def fail_optional(_context, _validated):
        raise RuntimeError("optional adapter failed")

    registry = ToolRegistry()
    registry.register(tool_spec("optional_failure", fail_optional))
    registry.register(TOOL_REGISTRY.get("search_local_papers"))
    monkeypatch.setattr(
        tool_module,
        "search_chunks_hybrid",
        lambda **_kwargs: [
            {
                "paper_id": paper_id,
                "page_number": 2,
                "chunk_id": chunk_id,
                "paper_title": "A Real Local Paper",
                "text": "A real local paper reports a verifiable experimental result.",
            }
        ],
    )
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    plan = plan_with(
        [
            PlannedNode(
                id="optional",
                tool_name="optional_failure",
                arguments={},
                reason="optional",
                completion_condition="optional",
                failure_strategy="skip",
                critical=False,
                retry_limit=0,
            ),
            PlannedNode(
                id="evidence",
                tool_name="search_local_papers",
                arguments={
                    "query": "verifiable result",
                    "limit": 1,
                    "paper_ids": [paper_id],
                },
                reason="evidence",
                completion_condition="evidence",
            ),
        ]
    )
    run = make_run(session_factory)
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        optional = (
            db.query(ResearchAgentNode)
            .filter_by(run_id=run.id, node_key="optional")
            .one()
        )
        assert stored.status == "completed_with_warnings"
        assert optional.status == "skipped"
        assert (
            db.query(ResearchAgentArtifact)
            .filter_by(run_id=run.id, artifact_type="strategy_decision")
            .count()
            == 1
        )
    finally:
        db.close()


def test_fallback_tool_success_produces_real_output(
    session_factory,
    paper_chunk,
    monkeypatch,
):
    paper_id, chunk_id = paper_chunk

    def fail_primary(_context, _validated):
        raise RuntimeError("primary adapter failed")

    registry = ToolRegistry()
    registry.register(tool_spec("primary_failure", fail_primary))
    registry.register(TOOL_REGISTRY.get("search_local_papers"))
    monkeypatch.setattr(
        tool_module,
        "search_chunks_hybrid",
        lambda **_kwargs: [
            {
                "paper_id": paper_id,
                "page_number": 2,
                "chunk_id": chunk_id,
                "paper_title": "A Real Local Paper",
                "text": "A real local paper reports a verifiable experimental result.",
            }
        ],
    )
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    plan = plan_with(
        [
            PlannedNode(
                id="fallback",
                tool_name="primary_failure",
                arguments={},
                reason="fallback",
                completion_condition="fallback",
                failure_strategy="fallback_tool",
                fallback_tool="search_local_papers",
                fallback_arguments={
                    "query": "verifiable result",
                    "limit": 1,
                    "paper_ids": [paper_id],
                },
                retry_limit=0,
            )
        ]
    )
    run = make_run(session_factory)
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        node = (
            db.query(ResearchAgentNode)
            .filter_by(run_id=run.id, node_key="fallback")
            .one()
        )
        calls = (
            db.query(ResearchAgentToolCall)
            .filter_by(run_id=run.id)
            .order_by(ResearchAgentToolCall.started_at.asc())
            .all()
        )
        assert stored.status == "completed"
        assert node.status == "completed"
        assert [call.tool_name for call in calls] == [
            "primary_failure",
            "search_local_papers",
        ]
        assert calls[-1].status == "completed"
        assert db.query(ResearchAgentEvidence).filter_by(run_id=run.id).count() == 1
    finally:
        db.close()


def test_zero_replan_budget_prevents_dynamic_extension(
    session_factory,
    monkeypatch,
):
    external_calls: list[str] = []

    def local_empty(_context, _validated):
        return SearchLocalPapersOutput(
            query="network traffic",
            result_count=0,
            source="local_hybrid_retrieval",
            sources=[],
            result_paper_ids=[],
        )

    def external_should_not_run(_context, _validated):
        external_calls.append("arxiv")
        raise AssertionError("replan budget is zero")

    registry = ToolRegistry()
    registry.register(
        tool_spec(
            "search_local_papers",
            local_empty,
            input_schema=tool_module.SearchLocalPapersInput,
            output_schema=SearchLocalPapersOutput,
        )
    )
    registry.register(
        tool_spec(
            "search_arxiv",
            external_should_not_run,
            input_schema=tool_module.SearchArxivInput,
            output_schema=SearchArxivOutput,
        )
    )
    plan = plan_with(
        [
            PlannedNode(
                id="local",
                tool_name="search_local_papers",
                arguments={"query": "network traffic", "limit": 5},
                reason="local",
                completion_condition="local",
            )
        ]
    )
    run = make_run(session_factory)
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        stored.max_replans = 0
        db.commit()
    finally:
        db.close()


def test_skipped_dependency_blocks_downstream_and_does_not_call_tool(
    session_factory,
    monkeypatch,
):
    dependent_calls = Mock(side_effect=AssertionError("dependent must not run"))

    def fail_optional(_context, _validated):
        raise RuntimeError("optional failure")

    registry = ToolRegistry()
    registry.register(tool_spec("optional_failure", fail_optional))
    registry.register(tool_spec("dependent_tool", dependent_calls))
    plan = plan_with(
        [
            PlannedNode(
                id="optional",
                tool_name="optional_failure",
                arguments={},
                reason="optional",
                completion_condition="optional",
                failure_strategy="skip",
                critical=False,
                retry_limit=0,
            ),
            PlannedNode(
                id="dependent",
                tool_name="dependent_tool",
                arguments={},
                depends_on=["optional"],
                reason="critical dependent",
                completion_condition="dependent",
                critical=True,
            ),
        ]
    )
    run = make_run(session_factory)
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    dependent_calls.assert_not_called()
    db = session_factory()
    try:
        dependent = (
            db.query(ResearchAgentNode)
            .filter_by(run_id=run.id, node_key="dependent")
            .one()
        )
        assert dependent.status == "blocked"
        assert dependent.error_message
    finally:
        db.close()


def test_recovery_does_not_reexecute_node_with_valid_output(
    session_factory,
    monkeypatch,
):
    completed_handler = Mock(side_effect=AssertionError("must be reused"))
    pending_handler = Mock(
        return_value=InspectLocalLibraryOutput(
            paper_count=0,
            result_count=0,
            papers=[],
        )
    )
    registry = ToolRegistry()
    registry.register(tool_spec("completed_tool", completed_handler))
    registry.register(tool_spec("pending_tool", pending_handler))
    plan = plan_with(
        [
            PlannedNode(
                id="completed",
                tool_name="completed_tool",
                arguments={},
                reason="completed",
                completion_condition="completed",
            ),
            PlannedNode(
                id="pending",
                tool_name="pending_tool",
                arguments={},
                reason="pending",
                completion_condition="pending",
            ),
        ]
    )
    run = make_run(session_factory)
    db = session_factory()
    try:
        node = ResearchAgentNode(
            run_id=run.id,
            node_key="completed",
            node_type="tool",
            tool_name="completed_tool",
            input_json="{}",
            dependencies_json="[]",
            reason="completed",
            completion_condition="completed",
            status="completed",
        )
        db.add(node)
        db.flush()
        db.add(
            ResearchAgentArtifact(
                run_id=run.id,
                artifact_type="node_output",
                content_json=json.dumps(
                    {
                        "node_id": node.id,
                        "node_key": "completed",
                        "tool_name": "completed_tool",
                        "output": {
                            "success": True,
                            "tool_name": "completed_tool",
                            "status": "empty_result",
                            "data": {"result_count": 0},
                            "summary": {},
                            "warnings": [],
                            "error": None,
                        },
                    }
                ),
                valid_source_count=0,
            )
        )
        db.commit()
    finally:
        db.close()
    lease = claim_run(
        session_factory,
        owner="recovery-test",
        run_id=run.id,
        max_concurrent_runs=2,
    )
    assert lease is not None

    class ActiveHeartbeat:
        def assert_active(self):
            return None

    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
        lease=lease,
        heartbeat=ActiveHeartbeat(),
    )
    completed_handler.assert_not_called()
    assert pending_handler.call_count == 1


def test_interrupted_tool_call_does_not_block_read_only_retry(
    session_factory,
    monkeypatch,
):
    handler_calls = Mock(
        return_value=InspectLocalLibraryOutput(
            paper_count=0,
            result_count=0,
            papers=[],
        )
    )
    registry = ToolRegistry()
    registry.register(tool_spec("recoverable_tool", handler_calls))
    plan = plan_with(
        [
            PlannedNode(
                id="recoverable",
                tool_name="recoverable_tool",
                arguments={},
                reason="recover",
                completion_condition="recover",
                retry_limit=0,
            )
        ]
    )
    run = make_run(session_factory)
    db = session_factory()
    try:
        node = ResearchAgentNode(
            run_id=run.id,
            node_key="recoverable",
            node_type="tool",
            tool_name="recoverable_tool",
            input_json="{}",
            dependencies_json="[]",
            reason="recover",
            completion_condition="recover",
            status="running",
        )
        db.add(node)
        db.flush()
        db.add(
            ResearchAgentToolCall(
                run_id=run.id,
                node_id=node.id,
                tool_name="recoverable_tool",
                attempt=1,
                arguments_summary_json="{}",
                status="interrupted",
                result_summary_json=json.dumps(
                    {"error_category": "internal_error"}
                ),
            )
        )
        db.commit()
    finally:
        db.close()
    lease = claim_run(
        session_factory,
        owner="recovery-worker",
        run_id=run.id,
        max_concurrent_runs=2,
    )
    assert lease is not None

    class ActiveHeartbeat:
        def assert_active(self):
            return None

    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
        lease=lease,
        heartbeat=ActiveHeartbeat(),
    )
    assert handler_calls.call_count == 1
    db = session_factory()
    try:
        calls = (
            db.query(ResearchAgentToolCall)
            .filter_by(run_id=run.id, tool_name="recoverable_tool")
            .order_by(ResearchAgentToolCall.started_at.asc())
            .all()
        )
        assert [call.status for call in calls] == ["interrupted", "completed"]
    finally:
        db.close()


def test_cancel_running_lease_preserves_completed_nodes(
    session_factory,
):
    run = make_run(session_factory)
    db = session_factory()
    try:
        completed = ResearchAgentNode(
            run_id=run.id,
            node_key="completed",
            node_type="tool",
            tool_name="inspect_local_library",
            input_json="{}",
            dependencies_json="[]",
            reason="completed",
            completion_condition="completed",
            status="completed",
        )
        pending = ResearchAgentNode(
            run_id=run.id,
            node_key="pending",
            node_type="tool",
            tool_name="inspect_local_library",
            input_json="{}",
            dependencies_json="[]",
            reason="pending",
            completion_condition="pending",
            status="pending",
        )
        db.add_all([completed, pending])
        db.commit()
    finally:
        db.close()


def test_tool_timeout_is_classified_and_retry_budget_is_honored(
    session_factory,
    monkeypatch,
):
    def slow_handler(_context, _validated):
        time.sleep(0.08)
        return InspectLocalLibraryOutput(
            paper_count=0,
            result_count=0,
            papers=[],
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="slow_tool",
            description="slow test tool",
            input_schema=tool_module.InspectLocalLibraryInput,
            output_schema=InspectLocalLibraryOutput,
            side_effect="read_only",
            timeout=0.01,
            max_attempts=2,
            max_calls_per_run=2,
            cost_class="local",
            handler=slow_handler,
        )
    )
    plan = plan_with(
        [
            PlannedNode(
                id="slow",
                tool_name="slow_tool",
                arguments={},
                reason="timeout",
                completion_condition="timeout",
                retry_limit=1,
            )
        ]
    )
    run = make_run(session_factory)
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        calls = (
            db.query(ResearchAgentToolCall)
            .filter_by(run_id=run.id, tool_name="slow_tool")
            .all()
        )
        assert len(calls) == 2
        assert all(call.status == "failed" for call in calls)
        assert all(
            json.loads(call.result_summary_json)["error_category"] == "timeout"
            for call in calls
        )
    finally:
        db.close()


def test_cancel_signal_reaches_cooperative_tool(
    session_factory,
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()

    def cooperative_handler(context, _validated):
        started.set()
        release.wait(timeout=5)
        if context.cancel_check():
            raise ToolExecutionError(
                "研究任务已取消。",
                code="AGENT_CANCELLED",
                retryable=False,
            )
        return InspectLocalLibraryOutput(
            paper_count=0,
            result_count=0,
            papers=[],
        )

    registry = ToolRegistry()
    registry.register(tool_spec("cooperative_tool", cooperative_handler))
    plan = plan_with(
        [
            PlannedNode(
                id="wait",
                tool_name="cooperative_tool",
                arguments={},
                reason="wait",
                completion_condition="wait",
                retry_limit=0,
            )
        ]
    )
    run = make_run(session_factory)
    lease = claim_run(
        session_factory,
        owner="cancel-worker",
        run_id=run.id,
        max_concurrent_runs=2,
    )
    assert lease is not None

    class ActiveHeartbeat:
        def assert_active(self):
            return None

    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    errors: list[Exception] = []

    def execute():
        try:
            execute_research_run(
                run.id,
                session_factory,
                registry=registry,
                planner=StaticPlanner(plan, registry),
                lease=lease,
                heartbeat=ActiveHeartbeat(),
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    assert started.wait(timeout=5)
    db = session_factory()
    try:
        cancelled = research_agent_service.cancel_research_run(db, run.id)
        assert cancelled.status == "cancelling"
    finally:
        db.close()
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        node = db.query(ResearchAgentNode).filter_by(run_id=run.id).one()
        assert stored.status == "cancelled"
        assert node.status == "cancelled"
    finally:
        db.close()


def test_run_total_timeout_stops_before_new_node(
    session_factory,
    monkeypatch,
):
    def delayed_handler(_context, _validated):
        return InspectLocalLibraryOutput(
            paper_count=0,
            result_count=0,
            papers=[],
        )

    registry = ToolRegistry()
    registry.register(tool_spec("delayed_tool", delayed_handler))
    plan = plan_with(
        [
            PlannedNode(
                id="delayed",
                tool_name="delayed_tool",
                arguments={},
                reason="delay",
                completion_condition="delay",
            )
        ]
    )
    run = make_run(session_factory)
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        stored.max_execution_seconds = 10
        stored.started_at = datetime.utcnow() - timedelta(seconds=20)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=""),
    )
    execute_research_run(
        run.id,
        session_factory,
        registry=registry,
        planner=StaticPlanner(plan, registry),
    )
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        node = db.query(ResearchAgentNode).filter_by(run_id=run.id).one()
        assert node.status == "pending"
        assert stored.status == "failed"
        assert "总执行时间预算" in (stored.error_message or "")
        assert db.query(ResearchAgentToolCall).filter_by(run_id=run.id).count() == 0
    finally:
        db.close()


def test_final_report_json_repair_uses_remaining_budget(monkeypatch):
    valid_report = {
        "title": "Evidence Report",
        "executive_summary": "The report is grounded in S1.",
        "sections": [
            {
                "heading": "Finding",
                "content": "The selected paper supports the finding.",
                "citation_ids": ["S1"],
            }
        ],
        "limitations": "Only one source is available.",
    }
    contents = [
        '{"title": "truncated report"',
        json.dumps(valid_report),
    ]

    class ReportLLM:
        def __init__(self):
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create),
            )

        def create(self, **kwargs):
            self.calls.append(kwargs)
            index = min(len(self.calls) - 1, len(contents) - 1)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=contents[index]),
                        finish_reason="stop",
                    )
                ]
            )

    llm = ReportLLM()
    monkeypatch.setattr(executor, "get_llm_client", lambda: llm)
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: SimpleNamespace(
            llm_api_key="configured",
            llm_model_name="test-model",
        ),
    )
    run = SimpleNamespace(
        goal="Compare evidence",
        topic="Evidence comparison",
        research_question="What does the evidence support?",
    )
    evidence = [
        SimpleNamespace(
            source_type="local_full_text",
            title="Paper",
            paper_id=1,
            page_number=2,
            chunk_id=3,
            arxiv_id=None,
            excerpt="Verifiable local evidence.",
        )
    ]
    consumed = []

    report, diagnostics = executor._llm_report_bounded(
        run,
        evidence,
        max_attempts=2,
        consume_llm_call=lambda: consumed.append(True),
    )

    assert report.title == "Evidence Report"
    assert diagnostics[0]["failure_kind"] == "json_parse_error"
    assert len(llm.calls) == 2
    assert len(consumed) == 2
    assert "Repair the supplied JSON only" in (
        llm.calls[1]["messages"][0]["content"]
    )


def test_compare_papers_reserves_final_report_llm_call():
    spec = TOOL_REGISTRY.get("compare_papers")

    assert spec.llm_calls_per_execution == 2
    assert spec.dynamic_llm_calls is True


def test_final_report_does_not_cross_run_deadline():
    run = SimpleNamespace(
        goal="Deadline",
        topic="Deadline",
        research_question="Can the report respect its deadline?",
    )
    evidence = [
        SimpleNamespace(
            source_type="local_full_text",
            title="Paper",
            paper_id=1,
            page_number=1,
            chunk_id=1,
            arxiv_id=None,
            excerpt="Evidence.",
        )
    ]
    consumed = Mock()

    with pytest.raises(executor.ReportGenerationError) as captured:
        executor._llm_report_bounded(
            run,
            evidence,
            max_attempts=1,
            consume_llm_call=consumed,
            deadline=datetime.utcnow() - timedelta(seconds=1),
        )

    assert captured.value.code == "AGENT_REPORT_TIME_BUDGET_EXHAUSTED"
    consumed.assert_not_called()
