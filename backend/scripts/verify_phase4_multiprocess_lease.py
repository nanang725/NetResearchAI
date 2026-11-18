from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.research_agent import (
    ResearchAgentArtifact,
    ResearchAgentNode,
    ResearchAgentRun,
)
from app.services.research_agent.executor import _install_worker_fence
from app.services.research_agent.worker import (
    LeaseLostError,
    RunLease,
    claim_run,
    reconcile_claimed_run,
)


def _session_factory(database_path: str):
    engine = create_engine(
        f"sqlite:///{Path(database_path).as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
        future=True,
    )
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def _claim_worker(
    database_path: str,
    owner: str,
    run_id: str,
    barrier,
    results,
) -> None:
    engine, factory = _session_factory(database_path)
    try:
        barrier.wait(timeout=20)
        lease = claim_run(
            factory,
            owner=owner,
            run_id=run_id,
            lease_seconds=30,
            max_concurrent_runs=2,
        )
        results.put(
            {
                "owner": owner,
                "claimed": lease is not None,
                "generation": lease.generation if lease else None,
            }
        )
    finally:
        engine.dispose()


def _stale_writer(
    database_path: str,
    lease: RunLease,
    results,
) -> None:
    engine, factory = _session_factory(database_path)
    db = factory()
    try:
        run = db.get(ResearchAgentRun, lease.run_id)
        run.error_message = "stale generation attempted this write"

        class Heartbeat:
            def assert_active(self) -> None:
                return None

        _install_worker_fence(db, lease, Heartbeat())
        try:
            db.commit()
            results.put({"stale_write_rejected": False})
        except LeaseLostError:
            db.rollback()
            results.put({"stale_write_rejected": True})
    finally:
        db.close()
        engine.dispose()


def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="netresearch-phase4-multiprocess-"))
    database_path = temp_dir / "lease-competition.db"
    engine, factory = _session_factory(str(database_path))
    Base.metadata.create_all(engine)
    db = factory()
    try:
        run = ResearchAgentRun(
            goal="Verify independent process lease competition",
            topic="",
            research_question="",
            status="pending",
            plan_version=0,
            replan_count=0,
            max_replans=1,
            max_nodes=3,
            max_tool_calls=4,
            max_llm_calls=4,
            max_arxiv_requests=0,
            max_execution_seconds=120,
            tool_calls_used=0,
            llm_calls_used=0,
            arxiv_requests_used=0,
            cancel_requested=False,
            lease_generation=0,
            resume_count=0,
        )
        db.add(run)
        db.flush()
        db.add(
            ResearchAgentNode(
                run_id=run.id,
                node_key="completed_node",
                node_type="tool",
                tool_name="inspect_local_library",
                input_json="{}",
                dependencies_json="[]",
                reason="already completed",
                completion_condition="valid output persisted",
                status="completed",
                completed_at=datetime.utcnow(),
            )
        )
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
        db.commit()
        run_id = run.id
    finally:
        db.close()
        engine.dispose()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_claim_worker,
            args=(str(database_path), owner, run_id, barrier, results),
        )
        for owner in ("worker-a", "worker-b")
    ]
    for worker in workers:
        worker.start()
    claims = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
    winners = [item for item in claims if item["claimed"]]

    engine, factory = _session_factory(str(database_path))
    db = factory()
    try:
        run = db.get(ResearchAgentRun, run_id)
        run.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
        engine.dispose()

    takeover = claim_run(
        factory,
        owner="worker-takeover",
        run_id=run_id,
        lease_seconds=30,
        max_concurrent_runs=2,
    )
    winner_lease = None
    if winners:
        winner_name = winners[0]["owner"]
        winner_lease = RunLease(
            run_id=run_id,
            owner=winner_name,
            generation=winners[0]["generation"],
            previous_status="pending",
        )

    stale_result = None
    if winner_lease is not None:
        stale_queue = context.Queue()
        stale = context.Process(
            target=_stale_writer,
            args=(str(database_path), winner_lease, stale_queue),
        )
        stale.start()
        stale_result = stale_queue.get(timeout=30)
        stale.join(timeout=30)

    db = factory()
    try:
        recovery = reconcile_claimed_run(db, takeover)
        completed_node = (
            db.query(ResearchAgentNode)
            .filter_by(run_id=run_id, node_key="completed_node")
            .one()
        )
        stored = db.get(ResearchAgentRun, run_id)
        report = {
            "temporary_database": str(database_path),
            "claims": claims,
            "single_winner": len(winners) == 1,
            "initial_generation": winners[0]["generation"] if winners else None,
            "takeover_generation": takeover.generation,
            "takeover_owner": stored.lease_owner,
            "stale_writer": stale_result,
            "completed_node_status": completed_node.status,
            "completed_nodes_reused": recovery["completed"],
            "stale_error_was_not_committed": (
                stored.error_message != "stale generation attempted this write"
            ),
        }
        complete = (
            report["single_winner"]
            and report["initial_generation"] == 1
            and report["takeover_generation"] == 2
            and report["takeover_owner"] == "worker-takeover"
            and report["stale_writer"] == {"stale_write_rejected": True}
            and report["completed_node_status"] == "completed"
            and "completed_node" in report["completed_nodes_reused"]
            and report["stale_error_was_not_committed"]
        )
        report["complete"] = complete
        output_path = temp_dir / "result.json"
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report["result_path"] = str(output_path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if complete else 1
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
