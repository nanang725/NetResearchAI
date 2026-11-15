from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.paper import (
    Paper,
    PaperChunk,
    PaperElement,
    PaperImportItem,
    PaperImportJob,
    PaperPage,
)
import app.services.ingestion_service as ingestion_module
import app.services.embedding_service as embedding_module
from app.services.ingestion_service import (
    LeaseFenceBusy,
    TaskStatusBusy,
    WorkerLease,
    WorkerLeaseLost,
    _claim_next_item,
    _fence_worker_lease,
    _record_fence_busy_deferral,
    _update_item,
)
from app.services.paper_service import PDF_CHECKPOINT_SCHEMA_VERSION


@pytest.fixture()
def worker_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'fencing.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 0.01},
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


def _create_running_item(
    factory,
    *,
    owner: str = "worker-a",
    generation: int = 1,
    expires_in: float = 60,
) -> tuple[int, int, str]:
    with factory() as db:
        paper = Paper(
            title="Lease Fenced Paper",
            file_hash=hashlib.sha256(b"lease-fenced-paper").hexdigest(),
            original_filename="lease-fenced.pdf",
            stored_filename="lease-fenced.pdf",
            file_path="unused-lease-fenced.pdf",
            file_size=100,
            content_type="application/pdf",
            page_count=1,
            char_count=0,
            parse_status="parsing",
            parse_checkpoint_version=PDF_CHECKPOINT_SCHEMA_VERSION,
            parse_source_hash=hashlib.sha256(b"lease-fenced-paper").hexdigest(),
            parse_source_size=100,
            parse_source_page_count=1,
            index_status="pending",
        )
        db.add(paper)
        db.flush()
        page = PaperPage(
            paper_id=paper.id,
            page_number=1,
            text="",
            char_count=0,
            checkpoint_status="processing",
            checkpoint_generation=generation,
            checkpoint_worker=owner,
        )
        db.add(page)
        job = PaperImportJob(
            kind="import",
            status="running",
            total_count=1,
        )
        db.add(job)
        db.flush()
        item = PaperImportItem(
            job_id=job.id,
            paper_id=paper.id,
            original_filename="lease-fenced.pdf",
            staged_path=None,
            file_size=100,
            status="running",
            current_stage="parsing",
            lease_owner=owner,
            lease_expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
            lease_generation=generation,
        )
        db.add(item)
        db.commit()
        return paper.id, page.id, item.id


def _commit_page(
    factory,
    paper_id: int,
    item_id: str,
    lease: WorkerLease,
    text_value: str,
) -> None:
    with factory() as db:
        _fence_worker_lease(db, lease, threading.Event())
        paper = db.query(Paper).filter_by(id=paper_id).one()
        page = db.query(PaperPage).filter_by(paper_id=paper_id, page_number=1).one()
        page.text = text_value
        page.char_count = len(text_value)
        page.parse_status = "text_extracted"
        page.checkpoint_status = "completed"
        page.checkpoint_generation = lease.generation
        page.checkpoint_worker = lease.owner
        db.add(
            PaperElement(
                paper_id=paper_id,
                page_id=page.id,
                page_number=1,
                element_index=0,
                element_type="paragraph",
                text=text_value,
            )
        )
        db.add(
            PaperChunk(
                paper_id=paper_id,
                page_number=1,
                chunk_index=0,
                text=text_value,
                content_type="paragraph",
            )
        )
        paper.parse_status = "parsed"
        db.commit()


def test_old_worker_cannot_commit_after_new_worker_takeover(
    worker_sessions,
):
    paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    old_lease = WorkerLease(item_id, "worker-a", 1)
    ready = threading.Event()
    proceed = threading.Event()
    errors = []

    def old_worker():
        ready.set()
        assert proceed.wait(5)
        try:
            _commit_page(
                worker_sessions,
                paper_id,
                item_id,
                old_lease,
                "old worker stale content",
            )
        except WorkerLeaseLost as exc:
            errors.append(exc)

    thread = threading.Thread(target=old_worker)
    thread.start()
    assert ready.wait(5)

    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        item.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    new_lease = _claim_next_item(worker_sessions, "worker-b")
    assert new_lease is not None
    assert new_lease.generation == 2

    proceed.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1
    with worker_sessions() as db:
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        assert page.checkpoint_status == "processing"
        assert page.text == ""
        assert db.query(PaperElement).filter_by(paper_id=paper_id).count() == 0
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == 0
        assert db.query(Paper).filter_by(id=paper_id).one().parse_status == "parsing"

    _commit_page(
        worker_sessions,
        paper_id,
        item_id,
        new_lease,
        "new worker valid content",
    )
    with worker_sessions() as db:
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        assert page.checkpoint_status == "completed"
        assert page.checkpoint_generation == 2
        assert page.text == "new worker valid content"
        assert db.query(PaperElement).filter_by(paper_id=paper_id).count() == 1
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == 1


def test_old_worker_cannot_mark_completed_page_failed(
    worker_sessions,
):
    paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    old_lease = WorkerLease(item_id, "worker-a", 1)
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        item.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    new_lease = _claim_next_item(worker_sessions, "worker-b")
    assert new_lease is not None
    _commit_page(
        worker_sessions,
        paper_id,
        item_id,
        new_lease,
        "completed by worker b",
    )

    with worker_sessions() as db:
        with pytest.raises(WorkerLeaseLost):
            _fence_worker_lease(db, old_lease, threading.Event())
        db.rollback()
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        assert page.checkpoint_status == "completed"
        assert page.checkpoint_generation == 2
        assert page.text == "completed by worker b"


def test_expired_lease_is_rejected_before_page_transaction(
    worker_sessions,
):
    paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        item.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        with pytest.raises(WorkerLeaseLost):
            _fence_worker_lease(db, lease, threading.Event())
        db.rollback()
    with worker_sessions() as db:
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        assert page.checkpoint_status == "processing"
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == 0


def test_active_page_write_transaction_serializes_takeover(
    worker_sessions,
):
    paper_id, _page_id, item_id = _create_running_item(
        worker_sessions,
        expires_in=0.3,
    )
    old_lease = WorkerLease(item_id, "worker-a", 1)
    write_locked = threading.Event()
    release_commit = threading.Event()
    commit_order = []

    def old_worker():
        with worker_sessions() as db:
            _fence_worker_lease(db, old_lease, threading.Event())
            write_locked.set()
            assert release_commit.wait(5)
            page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
            page.checkpoint_status = "completed"
            page.checkpoint_generation = 1
            page.text = "worker a committed first"
            page.char_count = len(page.text)
            db.commit()
            commit_order.append("worker-a-commit")

    thread = threading.Thread(target=old_worker)
    thread.start()
    assert write_locked.wait(5)
    takeover_done = threading.Event()
    takeover_results = []
    takeover_errors = []

    def new_worker():
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                lease = _claim_next_item(worker_sessions, "worker-b")
                if lease is not None:
                    takeover_results.append(lease)
                    commit_order.append("worker-b-claim")
                    break
                time.sleep(0.02)
            except Exception as exc:
                takeover_errors.append(exc)
                time.sleep(0.02)
        takeover_done.set()

    takeover_thread = threading.Thread(target=new_worker)
    takeover_thread.start()
    assert not takeover_done.wait(0.1)
    time.sleep(0.3)
    release_commit.set()
    thread.join(5)
    takeover_thread.join(5)

    assert not thread.is_alive()
    assert not takeover_thread.is_alive()
    assert all(
        "locked" in str(error).lower() or "busy" in str(error).lower()
        for error in takeover_errors
    )
    assert takeover_done.is_set()
    assert takeover_results
    assert takeover_results[0].generation == 2
    assert commit_order == ["worker-a-commit", "worker-b-claim"]


def test_busy_fence_retries_then_commits_page_without_failing_task(
    worker_sessions,
):
    paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()
    errors = []

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.2)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)

    def commit_worker():
        try:
            _commit_page(
                worker_sessions,
                paper_id,
                item_id,
                lease,
                "committed after busy retry",
            )
        except Exception as exc:
            errors.append(exc)

    committer = threading.Thread(target=commit_worker)
    committer.start()
    holder.join(5)
    committer.join(5)

    assert not holder.is_alive()
    assert not committer.is_alive()
    assert not errors
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        assert item.status == "running"
        assert item.current_stage == "parsing"
        assert item.error_message is None
        assert page.checkpoint_status == "completed"
        assert page.text == "committed after busy retry"
        assert db.query(PaperElement).filter_by(paper_id=paper_id).count() == 1
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == 1


def test_busy_fence_exhaustion_leaves_task_running_and_recoverable(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.35)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        with pytest.raises(LeaseFenceBusy):
            _fence_worker_lease(
                db,
                lease,
                threading.Event(),
                max_attempts=2,
                retry_base_seconds=0.02,
                max_wait_seconds=0.05,
            )

    _record_fence_busy_deferral(
        worker_sessions,
        item_id,
        lease,
        "SQLite remained busy",
    )
    holder.join(5)
    assert not holder.is_alive()
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "running"
        assert item.lease_owner == "worker-a"
        assert item.lease_generation == 1
        assert item.current_stage == "fence_busy"
        assert "remained busy" in item.error_message


def test_busy_wait_that_outlives_lease_becomes_lease_lost(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(
        worker_sessions,
        expires_in=0.15,
    )
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.3)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        with pytest.raises(WorkerLeaseLost):
            _fence_worker_lease(
                db,
                lease,
                threading.Event(),
                max_attempts=8,
                retry_base_seconds=0.03,
                max_wait_seconds=1,
            )
    holder.join(5)
    assert not holder.is_alive()


def test_busy_wait_with_takeover_becomes_lease_lost(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_lock_then_takeover():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.15)
            db.execute(
                text(
                    "UPDATE paper_import_items SET lease_owner=:owner, "
                    "lease_generation=lease_generation + 1, "
                    "lease_expires_at=:expires WHERE id=:id"
                ),
                {
                    "owner": "worker-b",
                    "expires": datetime.utcnow() + timedelta(seconds=60),
                    "id": item_id,
                },
            )
            db.commit()

    holder = threading.Thread(target=hold_lock_then_takeover)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        with pytest.raises(WorkerLeaseLost):
            _fence_worker_lease(
                db,
                lease,
                threading.Event(),
                max_attempts=6,
                retry_base_seconds=0.03,
                max_wait_seconds=1,
            )
    holder.join(5)
    assert not holder.is_alive()


def test_heartbeat_busy_does_not_set_lease_lost(
    worker_sessions,
    monkeypatch,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.15)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    monkeypatch.setattr(ingestion_module, "WORKER_HEARTBEAT_SECONDS", 0.01)
    stop_event = threading.Event()
    lease_lost = threading.Event()
    heartbeat = threading.Thread(
        target=ingestion_module._heartbeat_worker,
        args=(worker_sessions, lease, stop_event, lease_lost),
    )
    heartbeat.start()
    holder.join(5)
    time.sleep(0.25)
    stop_event.set()
    heartbeat.join(5)

    assert not holder.is_alive()
    assert not heartbeat.is_alive()
    assert not lease_lost.is_set()
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "running"
        assert item.lease_owner == "worker-a"
        assert item.lease_generation == 1


def test_progress_update_retries_busy_without_failing_task(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.2)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        updated = _update_item(
            db,
            item_id,
            current_stage="indexing",
            index_status="indexing",
            progress=72,
            lease=lease,
            defer_on_busy=True,
        )
    holder.join(5)

    assert updated is True
    assert not holder.is_alive()
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "running"
        assert item.current_stage == "indexing"
        assert item.index_status == "indexing"
        assert item.progress == 72
        assert item.error_message is None


def test_progress_update_busy_exhaustion_defers_without_failure(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.25)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        updated = _update_item(
            db,
            item_id,
            progress=88,
            lease=lease,
            defer_on_busy=True,
            max_attempts=2,
            max_wait_seconds=0.05,
        )
    holder.join(5)

    assert updated is False
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "running"
        assert item.lease_owner == "worker-a"
        assert item.progress == 0


def test_progress_update_after_lease_expiry_is_lease_lost(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(
        worker_sessions,
        expires_in=0.15,
    )
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.3)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        with pytest.raises(WorkerLeaseLost):
            _update_item(
                db,
                item_id,
                progress=60,
                lease=lease,
                defer_on_busy=True,
                max_attempts=8,
            )
    holder.join(5)
    assert not holder.is_alive()


def test_progress_update_after_takeover_is_lease_lost(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_lock_then_takeover():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.15)
            db.execute(
                text(
                    "UPDATE paper_import_items SET lease_owner=:owner, "
                    "lease_generation=lease_generation + 1, "
                    "lease_expires_at=:expires WHERE id=:id"
                ),
                {
                    "owner": "worker-b",
                    "expires": datetime.utcnow() + timedelta(seconds=60),
                    "id": item_id,
                },
            )
            db.commit()

    holder = threading.Thread(target=hold_lock_then_takeover)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        with pytest.raises(WorkerLeaseLost):
            _update_item(
                db,
                item_id,
                progress=60,
                lease=lease,
                defer_on_busy=True,
                max_attempts=6,
            )
    holder.join(5)
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.lease_owner == "worker-b"
        assert item.lease_generation == 2
        assert item.progress == 0


def test_final_succeeded_busy_stays_recoverable_and_is_not_faked(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    lock_held = threading.Event()

    def hold_write_lock():
        with worker_sessions() as db:
            db.execute(
                text(
                    "UPDATE paper_import_items "
                    "SET error_message = error_message WHERE id=:id"
                ),
                {"id": item_id},
            )
            lock_held.set()
            time.sleep(0.25)
            db.commit()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_held.wait(5)
    with worker_sessions() as db:
        with pytest.raises(TaskStatusBusy):
            _update_item(
                db,
                item_id,
                status="succeeded",
                current_stage="completed",
                progress=100,
                lease=lease,
                max_attempts=2,
                max_wait_seconds=0.05,
            )
    holder.join(5)

    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "running"
        assert item.current_stage == "parsing"
        assert item.progress == 0
        updated = _update_item(
            db,
            item_id,
            status="succeeded",
            current_stage="completed",
            progress=100,
            lease=lease,
        )
        assert updated is True
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "succeeded"
        assert item.progress == 100


def test_non_busy_database_error_is_not_swallowed(
    worker_sessions,
):
    _paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    with worker_sessions() as db:
        with pytest.raises(Exception) as error:
            _update_item(
                db,
                item_id,
                unknown_task_column=123,
                lease=lease,
            )
        assert not isinstance(error.value, (TaskStatusBusy, WorkerLeaseLost))
    with worker_sessions() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert item.status == "running"
        assert item.progress == 0


def test_deferred_index_progress_does_not_rollback_successful_chroma_write(
    worker_sessions,
    monkeypatch,
):
    paper_id, _page_id, item_id = _create_running_item(worker_sessions)
    lease = WorkerLease(item_id, "worker-a", 1)
    with worker_sessions() as db:
        paper = db.query(Paper).filter_by(id=paper_id).one()
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        paper.parse_status = "parsed"
        page.text = "Indexed network evidence."
        page.char_count = len(page.text)
        page.parse_status = "text_extracted"
        page.checkpoint_status = "completed"
        db.add(
            PaperChunk(
                paper_id=paper_id,
                page_number=1,
                page_chunk_index=0,
                chunk_index=0,
                text=page.text,
                content_type="paragraph",
            )
        )
        db.commit()

    class FakeCollection:
        def __init__(self):
            self.records = {}

        def get(self, **_kwargs):
            return {
                "ids": [],
                "documents": [],
                "metadatas": [],
                "embeddings": [],
            }

        def upsert(self, ids, embeddings, documents, metadatas):
            for vector_id in ids:
                self.records[vector_id] = True

        def delete(self, ids=None, where=None):
            return None

        def count(self):
            return len(self.records)

    collection = FakeCollection()
    monkeypatch.setattr(
        embedding_module,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        embedding_module,
        "encode_texts",
        lambda texts: [[1.0, float(index)] for index, _ in enumerate(texts)],
    )
    progress_values = []
    monkeypatch.setattr(
        ingestion_module,
        "_update_item",
        lambda *_args, **_kwargs: False,
    )

    def progress_callback(completed, total):
        progress_values.append((completed, total))
        with worker_sessions() as db:
            updated = ingestion_module._update_item(
                db,
                item_id,
                current_stage="indexing",
                index_status="indexing",
                progress=70,
                lease=lease,
                defer_on_busy=True,
                max_attempts=1,
                max_wait_seconds=0.01,
            )
        assert updated is False

    with worker_sessions() as db:
        result = embedding_module.index_paper(
            db,
            paper_id,
            progress_callback=progress_callback,
        )

    assert result["status"] == "indexed"
    assert progress_values
    assert collection.count() == 1
    with worker_sessions() as db:
        paper = db.query(Paper).filter_by(id=paper_id).one()
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        assert paper.index_status == "indexed"
        assert item.status == "running"
        assert item.progress == 0
