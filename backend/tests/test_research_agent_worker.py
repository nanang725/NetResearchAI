from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.research_agent import (
    ResearchAgentArtifact,
    ResearchAgentNode,
    ResearchAgentRun,
    ResearchAgentToolCall,
)
from app.services.research_agent.worker import (
    LeaseLostError,
    _claimable_run_ids,
    assert_lease,
    claim_run,
    reconcile_claimed_run,
    release_lease,
    renew_lease,
)
from app.services.research_agent.executor import _install_worker_fence


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'agent-worker.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
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


def _run(
    session_factory,
    *,
    status: str = "pending",
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    generation: int = 0,
) -> ResearchAgentRun:
    db = session_factory()
    try:
        run = ResearchAgentRun(
            goal="研究网络流量预测的方法与证据",
            topic="",
            research_question="",
            status=status,
            plan_version=0,
            replan_count=0,
            max_replans=1,
            max_nodes=8,
            max_tool_calls=12,
            max_llm_calls=4,
            max_arxiv_requests=3,
            max_execution_seconds=180,
            tool_calls_used=0,
            llm_calls_used=0,
            arxiv_requests_used=0,
            cancel_requested=status == "cancelled",
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            lease_generation=generation,
            resume_count=0,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        db.expunge(run)
        return run
    finally:
        db.close()


def test_lease_claim_is_atomic_and_blocks_second_worker(session_factory):
    run = _run(session_factory)

    def try_claim(owner):
        return claim_run(
            session_factory,
            owner=owner,
            run_id=run.id,
            max_concurrent_runs=2,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(try_claim, ["worker-a", "worker-b"])
        )
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert winners[0].generation == 1


def test_heartbeat_renews_and_expired_lease_can_be_taken_over(session_factory):
    run = _run(session_factory)
    first = claim_run(
        session_factory,
        owner="worker-a",
        run_id=run.id,
        lease_seconds=30,
    )
    assert first is not None
    db = session_factory()
    try:
        before = db.get(ResearchAgentRun, run.id).lease_expires_at
    finally:
        db.close()
    assert renew_lease(session_factory, first, lease_seconds=60)
    db = session_factory()
    try:
        after = db.get(ResearchAgentRun, run.id).lease_expires_at
        row = db.get(ResearchAgentRun, run.id)
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    assert after > before

    second = claim_run(
        session_factory,
        owner="worker-b",
        run_id=run.id,
        max_concurrent_runs=2,
    )
    assert second is not None
    assert second.generation == first.generation + 1
    assert second.previous_status == "running"
    assert renew_lease(session_factory, first, lease_seconds=30) is False
    assert release_lease(session_factory, first) is False
    assert release_lease(session_factory, second) is True

    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        assert stored.resume_count == 1
        with pytest.raises(LeaseLostError):
            assert_lease(db, first)
    finally:
        db.close()


def test_global_concurrency_limit_blocks_additional_run(session_factory):
    first_run = _run(session_factory)
    second_run = _run(session_factory)
    first = claim_run(
        session_factory,
        owner="worker-a",
        run_id=first_run.id,
        max_concurrent_runs=1,
    )
    assert first is not None
    blocked = claim_run(
        session_factory,
        owner="worker-b",
        run_id=second_run.id,
        max_concurrent_runs=1,
    )
    assert blocked is None
    release_lease(session_factory, first)
    allowed = claim_run(
        session_factory,
        owner="worker-b",
        run_id=second_run.id,
        max_concurrent_runs=1,
    )
    assert allowed is not None


@pytest.mark.parametrize(
    "status",
    [
        "draft",
        "completed",
        "completed_with_warnings",
        "partial",
        "failed",
        "cancelled",
    ],
)
def test_non_recoverable_statuses_are_never_claimed(session_factory, status):
    run = _run(session_factory, status=status)
    assert claim_run(
        session_factory,
        owner="worker",
        run_id=run.id,
    ) is None
    assert run.id not in _claimable_run_ids(session_factory)


def test_recovery_reuses_valid_output_and_requeues_unfinished_node(session_factory):
    run = _run(session_factory)
    db = session_factory()
    try:
        completed = ResearchAgentNode(
            run_id=run.id,
            node_key="completed_node",
            node_type="tool",
            tool_name="inspect_local_library",
            input_json="{}",
            dependencies_json="[]",
            reason="completed",
            completion_condition="done",
            status="completed",
        )
        unfinished = ResearchAgentNode(
            run_id=run.id,
            node_key="unfinished_node",
            node_type="tool",
            tool_name="inspect_local_library",
            input_json="{}",
            dependencies_json="[]",
            reason="unfinished",
            completion_condition="done",
            status="running",
        )
        db.add_all([completed, unfinished])
        db.flush()
        db.add(
            ResearchAgentArtifact(
                run_id=run.id,
                artifact_type="node_output",
                content_json=json.dumps(
                    {
                        "node_key": "completed_node",
                        "output": {
                            "success": True,
                            "status": "success",
                            "data": {"result_count": 1},
                        },
                    }
                ),
                valid_source_count=0,
            )
        )
        db.add(
            ResearchAgentToolCall(
                run_id=run.id,
                node_id=unfinished.id,
                tool_name="inspect_local_library",
                attempt=1,
                arguments_summary_json="{}",
                status="running",
            )
        )
        db.commit()
    finally:
        db.close()


def test_old_generation_commit_is_rejected_after_takeover(session_factory):
    run = _run(session_factory)
    first = claim_run(
        session_factory,
        owner="worker-a",
        run_id=run.id,
        max_concurrent_runs=2,
    )
    assert first is not None
    db = session_factory()
    try:
        stored = db.get(ResearchAgentRun, run.id)
        stored.error_message = "stale generation write"
        db.commit()

        expire = session_factory()
        try:
            row = expire.get(ResearchAgentRun, run.id)
            row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
            expire.commit()
        finally:
            expire.close()
        second = claim_run(
            session_factory,
            owner="worker-b",
            run_id=run.id,
            max_concurrent_runs=2,
        )
        assert second is not None

        stored = db.get(ResearchAgentRun, run.id)
        stored.error_message = "worker-a must not commit this"

        class ActiveHeartbeat:
            def assert_active(self):
                return None

        _install_worker_fence(db, first, ActiveHeartbeat())
        with pytest.raises(LeaseLostError):
            db.commit()
        db.rollback()
    finally:
        db.close()

    verify = session_factory()
    try:
        stored = verify.get(ResearchAgentRun, run.id)
        assert stored.error_message != "worker-a must not commit this"
        assert stored.lease_generation == 2
        assert stored.lease_owner == "worker-b"
    finally:
        verify.close()
