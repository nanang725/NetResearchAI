from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.research_agent.tools as tool_module
import app.api.research_agent as research_agent_api
from app.database import get_db
from app.database import Base
from app.core.exceptions import AppError, app_error_handler
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
    InspectLocalLibraryOutput,
    PlannerAnalysis,
    PlannerPlan,
    PlannedNode,
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
)


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
        artifact = db.query(ResearchAgentArtifact).filter_by(run_id=run.id).one()
        assert calls == ["local", "arxiv"]
        assert stored_run.replan_count == 1
        assert stored_run.plan_version == 2
        assert any(node.node_key == "dynamic_external_search" for node in nodes)
        assert evidence[0].source_type == "external_arxiv_abstract"
        assert artifact.valid_source_count == 1
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
