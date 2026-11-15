from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import pytest

from app.database import Base
from app.models.paper import Paper, PaperImportItem, PaperImportJob
import app.services.ingestion_service as ingestion_service
from app.services.ingestion_service import (
    WorkerLease,
    WorkerLeaseLost,
    _claim_next_item,
    _process_index_reconciliation,
    _reconcile_completed_rebuild,
    _update_item,
    create_index_rebuild_job,
    recover_interrupted_jobs,
    retry_import_item,
)


@pytest.fixture()
def isolated_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
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


def _create_job(
    factory,
    job_status: str,
    item_statuses: list[tuple[str, str | None]],
) -> tuple[str, list[str]]:
    with factory() as db:
        job = PaperImportJob(status=job_status, total_count=len(item_statuses))
        db.add(job)
        db.flush()
        items = []
        for index, (status, staged_path) in enumerate(item_statuses):
            item = PaperImportItem(
                job_id=job.id,
                original_filename=f"paper-{index}.pdf",
                staged_path=staged_path,
                file_size=1,
                status=status,
                current_stage=(
                    "completed" if status == "succeeded"
                    else "failed" if status == "failed"
                    else "queued" if status == "queued"
                    else "indexing"
                ),
                parse_status="parsed" if status == "succeeded" else "pending",
                index_status="indexed" if status == "succeeded" else "pending",
            )
            db.add(item)
            items.append(item)
        db.flush()
        db.commit()
        return job.id, [item.id for item in items]


def _create_papers(factory, count: int) -> list[int]:
    with factory() as db:
        paper_ids = []
        for index in range(count):
            paper = Paper(
                title=f"Repair Paper {index}",
                file_hash=hashlib.sha256(
                    f"repair-paper-{index}".encode("utf-8")
                ).hexdigest(),
                original_filename=f"repair-{index}.pdf",
                stored_filename=f"repair-{index}.pdf",
                file_path=f"unused-repair-{index}.pdf",
                file_size=1,
                content_type="application/pdf",
                page_count=1,
                char_count=10,
                parse_status="parsed",
                index_status="indexed",
            )
            db.add(paper)
            db.flush()
            paper_ids.append(paper.id)
        db.commit()
        return paper_ids


def _create_repair_job(factory, paper_ids: list[int]) -> tuple[str, dict[int, str]]:
    with factory() as db:
        job = PaperImportJob(
            kind="index_rebuild",
            status="running",
            total_count=len(paper_ids),
            completed_count=len(paper_ids),
        )
        db.add(job)
        db.flush()
        item_ids = {}
        for paper_id in paper_ids:
            paper = db.query(Paper).filter_by(id=paper_id).one()
            item = PaperImportItem(
                job_id=job.id,
                paper_id=paper_id,
                original_filename=paper.original_filename,
                staged_path=None,
                file_size=paper.file_size,
                status="succeeded",
                parse_status="parsed",
                index_status="indexed",
                current_stage="completed",
                progress=100,
            )
            db.add(item)
            db.flush()
            item_ids[paper_id] = item.id
        db.commit()
        return job.id, item_ids


def test_item_and_job_summary_commit_together(isolated_sessions):
    job_id, item_ids = _create_job(isolated_sessions, "running", [("running", None)])
    with isolated_sessions() as db:
        committed_states = []

        def capture_states(_session):
            with isolated_sessions() as verify_db:
                item_status = verify_db.execute(
                    text("SELECT status FROM paper_import_items WHERE id=:id"),
                    {"id": item_ids[0]},
                ).scalar_one()
                job_status = verify_db.execute(
                    text("SELECT status FROM paper_import_jobs WHERE id=:id"),
                    {"id": job_id},
                ).scalar_one()
                committed_states.append((item_status, job_status))

        event.listen(db, "after_commit", capture_states)
        _update_item(
            db,
            item_ids[0],
            status="succeeded",
            current_stage="completed",
            progress=100,
        )

        assert committed_states == [("succeeded", "completed")]


def test_item_and_job_summary_roll_back_together_on_commit_failure(isolated_sessions):
    job_id, item_ids = _create_job(isolated_sessions, "running", [("running", None)])
    with isolated_sessions() as db:
        def fail_commit(_session):
            raise RuntimeError("simulated commit failure")

        event.listen(db, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            _update_item(
                db,
                item_ids[0],
                status="succeeded",
                current_stage="completed",
                progress=100,
            )

        db.expire_all()
        item = db.query(PaperImportItem).filter_by(id=item_ids[0]).one()
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert item.status == "running"
        assert job.status == "running"
        assert job.completed_count == 0


def test_recovery_reconciles_stale_running_job_with_completed_items(isolated_sessions):
    job_id, _ = _create_job(isolated_sessions, "running", [("succeeded", None)])

    recover_interrupted_jobs(isolated_sessions)

    with isolated_sessions() as db:
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert job.status == "completed"
        assert job.completed_count == 1
        assert job.failed_count == 0


def test_recovery_aggregates_failed_items(isolated_sessions):
    job_id, _ = _create_job(
        isolated_sessions,
        "running",
        [("succeeded", None), ("failed", None)],
    )

    recover_interrupted_jobs(isolated_sessions)

    with isolated_sessions() as db:
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert job.status == "partial"
        assert job.completed_count == 1
        assert job.failed_count == 1


def test_recovery_requeues_unfinished_items_without_marking_job_complete(
    isolated_sessions,
    tmp_path,
):
    staged_path = tmp_path / "recoverable.pdf"
    staged_path.write_bytes(b"%PDF-test")
    job_id, item_ids = _create_job(
        isolated_sessions,
        "running",
        [("queued", str(staged_path)), ("running", str(staged_path))],
    )

    recover_interrupted_jobs(isolated_sessions)

    with isolated_sessions() as db:
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        items = (
            db.query(PaperImportItem)
            .filter(PaperImportItem.id.in_(item_ids))
            .order_by(PaperImportItem.original_filename.asc())
            .all()
        )
        assert job.status == "queued"
        assert job.completed_count == 0
        assert job.failed_count == 0
        assert {item.status for item in items} == {"queued"}


def test_workers_compete_atomically_for_one_queued_item(isolated_sessions):
    _, item_ids = _create_job(isolated_sessions, "queued", [("queued", None)])
    barrier = threading.Barrier(2)
    claims = []

    def claim(worker_id):
        barrier.wait()
        claims.append(_claim_next_item(isolated_sessions, worker_id))

    workers = [
        threading.Thread(target=claim, args=(f"worker-{index}",))
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    successful_claims = [claim for claim in claims if claim is not None]
    assert len(successful_claims) == 1
    assert successful_claims[0].item_id == item_ids[0]


def test_expired_lease_is_fenced_from_completing_after_takeover(isolated_sessions):
    from datetime import datetime, timedelta

    job_id, item_ids = _create_job(isolated_sessions, "queued", [("queued", None)])
    first_lease = _claim_next_item(isolated_sessions, "worker-one")
    assert first_lease is not None
    with isolated_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_ids[0]).one()
        item.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

    second_lease = _claim_next_item(isolated_sessions, "worker-two")
    assert second_lease is not None
    assert second_lease.generation == first_lease.generation + 1

    with isolated_sessions() as db:
        with pytest.raises(WorkerLeaseLost):
            _update_item(
                db,
                item_ids[0],
                lease=first_lease,
                status="succeeded",
                current_stage="completed",
                progress=100,
            )

    with isolated_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_ids[0]).one()
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert item.status == "running"
        assert item.lease_owner == "worker-two"
        assert item.lease_generation == second_lease.generation
        assert job.status == "running"


def test_recovery_preserves_a_nonexpired_worker_lease(isolated_sessions):
    from datetime import datetime, timedelta

    job_id, item_ids = _create_job(isolated_sessions, "running", [("running", None)])
    with isolated_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_ids[0]).one()
        item.lease_owner = "active-worker"
        item.lease_expires_at = datetime.utcnow() + timedelta(minutes=1)
        item.lease_generation = 1
        db.commit()

    recover_interrupted_jobs(isolated_sessions)

    with isolated_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_ids[0]).one()
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert item.status == "running"
        assert item.lease_owner == "active-worker"
        assert job.status == "running"


def test_expired_failed_staging_is_removed_but_recent_retry_file_is_kept(
    isolated_sessions,
    tmp_path,
):
    from datetime import datetime

    from app.services.ingestion_service import cleanup_expired_import_files

    staging_dir = tmp_path / ".imports"
    staging_dir.mkdir()
    expired_path = staging_dir / "expired.pdf"
    recent_path = staging_dir / "recent.pdf"
    orphan_path = staging_dir / "orphan.pdf"
    expired_path.write_bytes(b"%PDF-expired")
    recent_path.write_bytes(b"%PDF-recent")
    orphan_path.write_bytes(b"%PDF-orphan")
    import os

    os.utime(orphan_path, (1, 1))
    job_id, item_ids = _create_job(
        isolated_sessions,
        "failed",
        [("failed", str(expired_path)), ("failed", str(recent_path))],
    )
    with isolated_sessions() as db:
        db.execute(
            text("UPDATE paper_import_items SET updated_at=:value WHERE id=:id"),
            {"value": "2025-01-01 00:00:00", "id": item_ids[0]},
        )
        db.execute(
            text("UPDATE paper_import_items SET updated_at=:value WHERE id=:id"),
            {"value": "2025-12-31 00:00:00", "id": item_ids[1]},
        )
        db.commit()

    cleanup_expired_import_files(
        isolated_sessions,
        staging_dir,
        now=datetime(2026, 1, 1),
    )

    with isolated_sessions() as db:
        expired = db.query(PaperImportItem).filter_by(id=item_ids[0]).one()
        recent = db.query(PaperImportItem).filter_by(id=item_ids[1]).one()
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert not expired_path.exists()
        assert not orphan_path.exists()
        assert expired.staged_path is None
        assert "超过 30 天保留期" in expired.error_message
        assert recent_path.is_file()
        assert recent.staged_path == str(recent_path)
        assert job.status == "failed"


def test_final_recheck_failure_is_attributed_to_the_affected_paper(
    isolated_sessions,
    monkeypatch,
):
    paper_a, paper_b = _create_papers(isolated_sessions, 2)
    job_id, item_ids = _create_repair_job(
        isolated_sessions,
        [paper_a, paper_b],
    )
    monkeypatch.setattr(
        ingestion_service,
        "reconcile_index_consistency",
        lambda *_args, **_kwargs: {
            "consistent": False,
            "affected_paper_ids": [paper_a],
            "missing_vector_count": 0,
            "stale_vector_count": 1,
            "orphan_vector_count": 0,
            "unverifiable_vector_count": 0,
        },
    )

    _reconcile_completed_rebuild(isolated_sessions, item_ids[paper_b])

    with isolated_sessions() as db:
        item_a = db.query(PaperImportItem).filter_by(id=item_ids[paper_a]).one()
        item_b = db.query(PaperImportItem).filter_by(id=item_ids[paper_b]).one()
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert item_a.status == "failed"
        assert item_b.status == "succeeded"
        assert job.status == "partial"

        retry_import_item(db, job_id, item_a.id)
        assert db.query(PaperImportItem).filter_by(id=item_a.id).one().status == "queued"
        assert db.query(PaperImportItem).filter_by(id=item_b.id).one().status == "succeeded"

    lease = _claim_next_item(isolated_sessions, "repair-worker")
    assert lease is not None
    assert lease.item_id == item_ids[paper_a]


def test_final_recheck_creates_failed_item_when_affected_paper_has_no_item(
    isolated_sessions,
    monkeypatch,
):
    paper_a, paper_b = _create_papers(isolated_sessions, 2)
    job_id, item_ids = _create_repair_job(isolated_sessions, [paper_b])
    monkeypatch.setattr(
        ingestion_service,
        "reconcile_index_consistency",
        lambda *_args, **_kwargs: {
            "consistent": False,
            "affected_paper_ids": [paper_a],
            "missing_vector_count": 1,
            "stale_vector_count": 0,
            "orphan_vector_count": 0,
            "unverifiable_vector_count": 0,
        },
    )

    _reconcile_completed_rebuild(isolated_sessions, item_ids[paper_b])

    with isolated_sessions() as db:
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        items = (
            db.query(PaperImportItem)
            .filter_by(job_id=job_id)
            .order_by(PaperImportItem.paper_id.asc())
            .all()
        )
        assert job.status == "partial"
        assert job.total_count == 2
        assert job.failed_count == 1
        assert [(item.paper_id, item.status) for item in items] == [
            (paper_a, "failed"),
            (paper_b, "succeeded"),
        ]


def test_reconciliation_task_failure_does_not_mark_task_succeeded(
    isolated_sessions,
    monkeypatch,
):
    with isolated_sessions() as db:
        job = PaperImportJob(
            kind="index_rebuild",
            status="running",
            total_count=1,
        )
        db.add(job)
        db.flush()
        item = PaperImportItem(
            job_id=job.id,
            original_filename="索引全局最终复查",
            staged_path=None,
            file_size=0,
            status="running",
            current_stage="reconcile",
            lease_owner="worker-reconcile",
            lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
            lease_generation=1,
        )
        db.add(item)
        db.commit()
        job_id = job.id
        item_id = item.id

    monkeypatch.setattr(
        ingestion_service,
        "reconcile_index_consistency",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated global orphan recheck failure")
        ),
    )
    _process_index_reconciliation(
        isolated_sessions,
        item_id,
        WorkerLease(item_id, "worker-reconcile", 1),
        threading.Event(),
    )

    with isolated_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        job = db.query(PaperImportJob).filter_by(id=job_id).one()
        assert item.status == "failed"
        assert job.status == "failed"
        assert "global orphan recheck failure" in item.error_message


def test_concurrent_duplicate_repair_requests_reuse_one_active_job(
    isolated_sessions,
):
    paper_id, = _create_papers(isolated_sessions, 1)
    barrier = threading.Barrier(2)
    jobs = []
    errors = []

    def request_repair():
        try:
            with isolated_sessions() as db:
                barrier.wait()
                jobs.append(
                    create_index_rebuild_job(
                        db,
                        paper_ids=[paper_id],
                    ).id
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=request_repair) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(jobs) == 2
    assert len(set(jobs)) == 1
    with isolated_sessions() as db:
        assert db.query(PaperImportJob).count() == 1


def test_different_repair_targets_are_independent_and_history_does_not_block(
    isolated_sessions,
    monkeypatch,
):
    paper_a, paper_b = _create_papers(isolated_sessions, 2)
    with isolated_sessions() as db:
        job_a = create_index_rebuild_job(db, paper_ids=[paper_a])
    with isolated_sessions() as db:
        job_b = create_index_rebuild_job(db, paper_ids=[paper_b])
    assert job_a.id != job_b.id

    with isolated_sessions() as db:
        item = db.query(PaperImportItem).filter_by(job_id=job_a.id).one()
        item.status = "succeeded"
        item.current_stage = "completed"
        item.index_status = "indexed"
        item.progress = 100
        job_a_item_id = item.id
        ingestion_service._refresh_job_state(db, job_a.id)
        db.commit()
        assert db.query(PaperImportJob).filter_by(id=job_a.id).one().status == "running"

    monkeypatch.setattr(
        ingestion_service,
        "reconcile_index_consistency",
        lambda *_args, **_kwargs: {
            "consistent": True,
            "affected_paper_ids": [],
            "missing_vector_count": 0,
            "stale_vector_count": 0,
            "orphan_vector_count": 0,
            "unverifiable_vector_count": 0,
        },
    )
    _reconcile_completed_rebuild(
        isolated_sessions,
        job_a_item_id,
    )
    with isolated_sessions() as db:
        assert db.query(PaperImportJob).filter_by(id=job_a.id).one().status == "completed"

    with isolated_sessions() as db:
        replacement = create_index_rebuild_job(db, paper_ids=[paper_a])
    assert replacement.id != job_a.id
    with isolated_sessions() as db:
        assert db.query(PaperImportJob).count() == 3
