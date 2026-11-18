from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from uuid import uuid4

from sqlalchemy import case, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.research_agent import (
    ResearchAgentArtifact,
    ResearchAgentNode,
    ResearchAgentRun,
    ResearchAgentToolCall,
)


TERMINAL_RUN_STATUSES = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "cancelled",
}


class LeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunLease:
    run_id: str
    owner: str
    generation: int
    previous_status: str


_ACTIVE_THREADS: dict[str, threading.Thread] = {}
_ACTIVE_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.utcnow()


def _lease_condition(
    run_id: str,
    owner: str,
    generation: int,
    now: datetime,
):
    return (
        ResearchAgentRun.id == run_id,
        ResearchAgentRun.lease_owner == owner,
        ResearchAgentRun.lease_generation == generation,
        ResearchAgentRun.lease_expires_at.is_not(None),
        ResearchAgentRun.lease_expires_at > now,
    )


def claim_run(
    session_factory: Callable[[], Session],
    *,
    owner: str,
    run_id: str | None = None,
    lease_seconds: int | None = None,
    max_concurrent_runs: int | None = None,
) -> RunLease | None:
    settings = get_settings()
    lease_seconds = lease_seconds or settings.agent_lease_seconds
    max_concurrent_runs = (
        max_concurrent_runs or settings.agent_max_concurrent_runs
    )
    now = _now()
    db = session_factory()
    try:
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            try:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError:
                db.rollback()
                return None
        active_leases = (
            db.query(ResearchAgentRun)
            .filter(
                ResearchAgentRun.lease_owner.is_not(None),
                ResearchAgentRun.lease_expires_at > now,
                ResearchAgentRun.status.in_(
                    ("running", "cancelling")
                ),
            )
            .count()
        )
        if active_leases >= max_concurrent_runs:
            db.rollback()
            return None

        claimable = or_(
            ResearchAgentRun.status == "pending",
            (
                ResearchAgentRun.status.in_(("running", "cancelling"))
                & or_(
                    ResearchAgentRun.lease_expires_at.is_(None),
                    ResearchAgentRun.lease_expires_at <= now,
                )
            ),
        )
        query = db.query(ResearchAgentRun).filter(claimable)
        if run_id is not None:
            query = query.filter(ResearchAgentRun.id == run_id)
        candidate = (
            query.order_by(ResearchAgentRun.created_at.asc())
            .with_for_update()
            .first()
        )
        if candidate is None:
            db.rollback()
            return None

        previous_status = candidate.status
        updated = (
            db.query(ResearchAgentRun)
            .filter(
                ResearchAgentRun.id == candidate.id,
                claimable,
            )
            .update(
                {
                    ResearchAgentRun.lease_owner: owner,
                    ResearchAgentRun.lease_expires_at: (
                        now + timedelta(seconds=lease_seconds)
                    ),
                    ResearchAgentRun.lease_generation: (
                        ResearchAgentRun.lease_generation + 1
                    ),
                    ResearchAgentRun.heartbeat_at: now,
                    ResearchAgentRun.resume_count: case(
                        (
                            ResearchAgentRun.status == "running",
                            ResearchAgentRun.resume_count + 1,
                        ),
                        else_=ResearchAgentRun.resume_count,
                    ),
                    ResearchAgentRun.status: "running",
                    ResearchAgentRun.started_at: case(
                        (
                            ResearchAgentRun.started_at.is_(None),
                            now,
                        ),
                        else_=ResearchAgentRun.started_at,
                    ),
                    ResearchAgentRun.error_message: None,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            return None
        db.commit()
        db.refresh(candidate)
        return RunLease(
            run_id=candidate.id,
            owner=owner,
            generation=candidate.lease_generation,
            previous_status=previous_status,
        )
    finally:
        db.close()


def renew_lease(
    session_factory: Callable[[], Session],
    lease: RunLease,
    *,
    lease_seconds: int,
) -> bool:
    now = _now()
    db = session_factory()
    try:
        updated = (
            db.query(ResearchAgentRun)
            .filter(
                *_lease_condition(
                    lease.run_id,
                    lease.owner,
                    lease.generation,
                    now,
                ),
                ResearchAgentRun.status.in_(("running", "cancelling")),
            )
            .update(
                {
                    ResearchAgentRun.lease_expires_at: (
                        now + timedelta(seconds=lease_seconds)
                    ),
                    ResearchAgentRun.heartbeat_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            return False
        db.commit()
        return True
    finally:
        db.close()


def assert_lease(
    db: Session,
    lease: RunLease,
    *,
    allowed_statuses: tuple[str, ...] = ("running", "cancelling"),
) -> None:
    now = _now()
    active = (
        db.query(ResearchAgentRun.id)
        .filter(
            *_lease_condition(
                lease.run_id,
                lease.owner,
                lease.generation,
                now,
            ),
            ResearchAgentRun.status.in_(allowed_statuses),
        )
        .first()
    )
    if active is None:
        raise LeaseLostError(
            f"Run {lease.run_id} lease generation "
            f"{lease.generation} is no longer active."
        )


def release_lease(
    session_factory: Callable[[], Session],
    lease: RunLease,
) -> bool:
    db = session_factory()
    try:
        updated = (
            db.query(ResearchAgentRun)
            .filter(
                ResearchAgentRun.id == lease.run_id,
                ResearchAgentRun.lease_owner == lease.owner,
                ResearchAgentRun.lease_generation == lease.generation,
            )
            .update(
                {
                    ResearchAgentRun.lease_owner: None,
                    ResearchAgentRun.lease_expires_at: None,
                    ResearchAgentRun.heartbeat_at: _now(),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return updated == 1
    finally:
        db.close()


class LeaseHeartbeat:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        lease: RunLease,
    ) -> None:
        settings = get_settings()
        self.session_factory = session_factory
        self.lease = lease
        self.interval = settings.agent_heartbeat_seconds
        self.lease_seconds = settings.agent_lease_seconds
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"research-agent-heartbeat-{lease.run_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(1, self.interval + 1))

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                if not renew_lease(
                    self.session_factory,
                    self.lease,
                    lease_seconds=self.lease_seconds,
                ):
                    self.lost_event.set()
                    return
            except Exception:
                self.lost_event.set()
                return

    def assert_active(self) -> None:
        if self.lost_event.is_set():
            raise LeaseLostError("Research agent heartbeat lease was lost.")


def _record_recovery_artifact(
    db: Session,
    lease: RunLease,
    *,
    completed_nodes: list[str],
    interrupted_nodes: list[str],
) -> None:
    db.add(
        ResearchAgentArtifact(
            run_id=lease.run_id,
            artifact_type="run_recovery",
            content_json=json.dumps(
                {
                    "reason": (
                        "expired_lease_takeover"
                        if lease.previous_status == "running"
                        else "pending_dispatch"
                    ),
                    "previous_status": lease.previous_status,
                    "generation": lease.generation,
                    "completed_nodes_reused": completed_nodes,
                    "unfinished_nodes": interrupted_nodes,
                },
                ensure_ascii=False,
            ),
            valid_source_count=0,
        )
    )


def reconcile_claimed_run(
    db: Session,
    lease: RunLease,
) -> dict[str, list[str]]:
    now = _now()
    assert_lease(db, lease)
    output_nodes = set()
    for row in (
        db.query(ResearchAgentArtifact)
        .filter(
            ResearchAgentArtifact.run_id == lease.run_id,
            ResearchAgentArtifact.artifact_type == "node_output",
        )
        .order_by(ResearchAgentArtifact.created_at.asc())
        .all()
    ):
        payload = json.loads(row.content_json)
        output = payload.get("output")
        node_key = payload.get("node_key")
        if (
            isinstance(node_key, str)
            and isinstance(output, dict)
            and output.get("success") is True
            and output.get("status") in {"success", "empty_result"}
            and isinstance(output.get("data"), dict)
        ):
            output_nodes.add(node_key)
    completed: list[str] = []
    unfinished: list[str] = []
    for node in (
        db.query(ResearchAgentNode)
        .filter(ResearchAgentNode.run_id == lease.run_id)
        .all()
    ):
        if node.status == "completed":
            if node.node_key in output_nodes:
                completed.append(node.node_key)
            else:
                node.status = "failed"
                node.error_message = (
                    "节点标记为完成但缺少有效 node_output Artifact。"
                )
                node.completed_at = node.completed_at or now
            continue
        if node.status == "running":
            if node.node_key in output_nodes:
                node.status = "completed"
                node.completed_at = node.completed_at or now
                node.error_message = None
                completed.append(node.node_key)
            else:
                node.status = "pending"
                node.error_message = "Worker 中断，等待恢复执行。"
                unfinished.append(node.node_key)
    for call in (
        db.query(ResearchAgentToolCall)
        .filter(
            ResearchAgentToolCall.run_id == lease.run_id,
            ResearchAgentToolCall.status == "running",
        )
        .all()
    ):
        call.status = "interrupted"
        call.completed_at = now
        call.error_message = "Worker lease expired before tool call committed."
        call.result_summary_json = json.dumps(
            {
                "success": False,
                "status": "error",
                "error_category": "internal_error",
                "error_code": "AGENT_WORKER_INTERRUPTED",
                "retryable": True,
            },
            ensure_ascii=False,
        )
    _record_recovery_artifact(
        db,
        lease,
        completed_nodes=completed,
        interrupted_nodes=unfinished,
    )
    db.commit()
    return {"completed": completed, "unfinished": unfinished}


def _claimable_run_ids(session_factory: Callable[[], Session]) -> list[str]:
    now = _now()
    db = session_factory()
    try:
        rows = (
            db.query(ResearchAgentRun.id)
            .filter(
                or_(
                    ResearchAgentRun.status == "pending",
                    (
                        ResearchAgentRun.status.in_(("running", "cancelling"))
                        & or_(
                            ResearchAgentRun.lease_expires_at.is_(None),
                            ResearchAgentRun.lease_expires_at <= now,
                        )
                    ),
                )
            )
            .order_by(ResearchAgentRun.created_at.asc())
            .all()
        )
        return [row[0] for row in rows]
    finally:
        db.close()


def start_agent_run(
    run_id: str,
    session_factory: Callable[[], Session],
) -> None:
    with _ACTIVE_LOCK:
        existing = _ACTIVE_THREADS.get(run_id)
        if existing is not None and existing.is_alive():
            return

        def worker() -> None:
            from app.services.research_agent.executor import execute_research_run

            lease = None
            heartbeat = None
            try:
                lease = claim_run(
                    session_factory,
                    owner=uuid4().hex,
                    run_id=run_id,
                )
                if lease is None:
                    return
                heartbeat = LeaseHeartbeat(session_factory, lease)
                heartbeat.start()
                execute_research_run(
                    run_id,
                    session_factory,
                    lease=lease,
                    heartbeat=heartbeat,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.stop()
                if lease is not None:
                    release_lease(session_factory, lease)
                with _ACTIVE_LOCK:
                    _ACTIVE_THREADS.pop(run_id, None)

        thread = threading.Thread(
            target=worker,
            name=f"research-agent-worker-{run_id[:8]}",
            daemon=True,
        )
        _ACTIVE_THREADS[run_id] = thread
        thread.start()


def run_research_agent_dispatcher(
    session_factory: Callable[[], Session],
    stop_event: threading.Event,
) -> None:
    settings = get_settings()
    while not stop_event.wait(settings.agent_poll_seconds):
        try:
            # Startup recovery and steady-state dispatch share the same
            # pending/expired-running scan. Draft and terminal runs are excluded.
            for run_id in _claimable_run_ids(session_factory):
                start_agent_run(run_id, session_factory)
        except Exception:
            time.sleep(settings.agent_poll_seconds)
