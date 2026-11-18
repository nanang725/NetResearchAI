from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from sqlalchemy import and_, func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import (
    Paper,
    PaperImportItem,
    PaperImportJob,
    PaperIndexRepairLock,
)
from app.services.embedding_service import (
    audit_paper_index_consistency,
    index_paper,
    reconcile_index_consistency,
)
from app.services.paper_service import (
    create_or_resume_paper,
    get_paper_parse_completeness,
)

logger = logging.getLogger(__name__)
IMPORT_DIRECTORY = ".imports"
FAILED_IMPORT_RETENTION_DAYS = 30
_IMPORT_RETRY_LOCK = threading.Lock()
_INDEX_REPAIR_CREATION_LOCK = threading.Lock()
WORKER_LEASE_SECONDS = 90
WORKER_HEARTBEAT_SECONDS = 20
LEASE_FENCE_MAX_ATTEMPTS = 5
LEASE_FENCE_RETRY_BASE_SECONDS = 0.05
LEASE_FENCE_MAX_WAIT_SECONDS = 1.0
LEASE_HEARTBEAT_BUSY_ATTEMPTS = 3
TASK_STATUS_BUSY_MAX_ATTEMPTS = 4
TASK_STATUS_BUSY_MAX_WAIT_SECONDS = 0.6


@dataclass(frozen=True)
class WorkerLease:
    item_id: str
    owner: str
    generation: int


class WorkerLeaseLost(RuntimeError):
    pass


class LeaseFenceBusy(RuntimeError):
    pass


class TaskStatusBusy(RuntimeError):
    pass


def create_import_job(
    db: Session,
    staged_files: list[dict],
) -> PaperImportJob:
    job = PaperImportJob(total_count=len(staged_files))
    db.add(job)
    db.flush()
    for staged in staged_files:
        db.add(
            PaperImportItem(
                job_id=job.id,
                original_filename=staged["filename"],
                staged_path=str(staged["path"]) if staged.get("path") else None,
                file_size=staged.get("size", 0),
                status=staged.get("status", "queued"),
                current_stage=staged.get(
                    "current_stage",
                    "queued" if staged.get("status", "queued") == "queued" else "failed",
                ),
                parse_status=staged.get(
                    "parse_status",
                    "pending" if staged.get("status", "queued") == "queued" else "failed",
                ),
                error_message=staged.get("error_message"),
            )
        )
    try:
        _refresh_job_state(db, job.id)
        db.commit()
        db.refresh(job)
        return job
    except Exception:
        db.rollback()
        raise


def _build_index_rebuild_job(
    db: Session,
    papers: list[Paper],
    include_reconciliation: bool,
) -> PaperImportJob:
    job = PaperImportJob(
        kind="index_rebuild",
        status="queued" if papers or include_reconciliation else "completed",
        total_count=len(papers),
        completed_count=0,
    )
    db.add(job)
    db.flush()
    for paper in papers:
        db.add(
            PaperImportItem(
                job_id=job.id,
                paper_id=paper.id,
                original_filename=paper.original_filename,
                staged_path=None,
                file_size=paper.file_size,
                parse_status=paper.parse_status,
                index_status="pending",
                current_stage="queued",
            )
        )
    if not papers and include_reconciliation:
        db.add(
            PaperImportItem(
                job_id=job.id,
                original_filename="索引一致性检查",
                staged_path=None,
                file_size=0,
                current_stage="reconcile",
            )
        )
        job.total_count = 1
        job.status = "queued"
    return job


def _index_repair_target_key(
    paper_ids: list[int],
    include_reconciliation: bool,
) -> str:
    normalized_ids = sorted({int(paper_id) for paper_id in paper_ids})
    payload = {
        "paper_ids": normalized_ids,
        "reconcile_only": bool(include_reconciliation and not normalized_ids),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_index_repair_job(
    db: Session,
    target_key: str,
) -> PaperImportJob | None:
    lock = (
        db.query(PaperIndexRepairLock)
        .filter(PaperIndexRepairLock.target_key == target_key)
        .first()
    )
    if lock is None:
        return None
    job = (
        db.query(PaperImportJob)
        .filter(PaperImportJob.id == lock.job_id)
        .first()
    )
    if job is not None and job.status in {"queued", "running"}:
        return job
    db.delete(lock)
    db.flush()
    return None


def create_index_rebuild_job(
    db: Session,
    paper_ids: list[int] | None = None,
    include_reconciliation: bool = False,
) -> PaperImportJob:
    papers_query = db.query(Paper)
    if paper_ids is not None:
        papers_query = papers_query.filter(Paper.id.in_(paper_ids))
    papers = papers_query.order_by(Paper.id.asc()).all()
    target_key = _index_repair_target_key(
        [int(paper.id) for paper in papers],
        include_reconciliation,
    )

    with _INDEX_REPAIR_CREATION_LOCK:
        existing = _active_index_repair_job(db, target_key)
        if existing is not None:
            return existing

        job = _build_index_rebuild_job(db, papers, include_reconciliation)
        if job.status in {"queued", "running"}:
            db.add(
                PaperIndexRepairLock(
                    target_key=target_key,
                    job_id=job.id,
                )
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _active_index_repair_job(db, target_key)
            if existing is not None:
                return existing
            raise
        db.refresh(job)
        return job


def serialize_import_item(item: PaperImportItem, job_kind: str) -> dict:
    if job_kind == "index_rebuild":
        retryable = True
    else:
        retryable = _safe_staged_file_exists(item.staged_path)
    return {
        "id": item.id,
        "original_filename": item.original_filename,
        "paper_id": item.paper_id,
        "status": item.status,
        "parse_status": item.parse_status,
        "index_status": item.index_status,
        "current_stage": item.current_stage,
        "progress": item.progress,
        "attempts": item.attempts,
        "error_message": item.error_message,
        "file_size": item.file_size,
        "can_retry": item.status == "failed" and retryable,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _import_job_progress(job: PaperImportJob) -> int:
    if job.total_count <= 0:
        return 100
    processed_count = max(
        0,
        min(
            job.total_count,
            job.completed_count + job.failed_count,
        ),
    )
    return min(100, int(processed_count * 100 / job.total_count))


def serialize_import_job(job: PaperImportJob, include_items: bool = True) -> dict:
    items = job.items if include_items else []
    data = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "total_count": job.total_count,
        "completed_count": job.completed_count,
        "failed_count": job.failed_count,
        "queued_count": sum(item.status == "queued" for item in items),
        "running_count": sum(item.status == "running" for item in items),
        "progress": _import_job_progress(job),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
    if include_items:
        data["items"] = [serialize_import_item(item, job.kind) for item in items]
    return data


def get_import_job(db: Session, job_id: str) -> PaperImportJob:
    job = db.query(PaperImportJob).filter(PaperImportJob.id == job_id).first()
    if job is None:
        raise AppError("导入任务不存在。", code="IMPORT_JOB_NOT_FOUND", status_code=404)
    return job


def list_import_jobs(db: Session, limit: int = 50) -> list[PaperImportJob]:
    return (
        db.query(PaperImportJob)
        .order_by(PaperImportJob.created_at.desc())
        .limit(limit)
        .all()
    )


def retry_import_job(db: Session, job_id: str) -> PaperImportJob:
    with _IMPORT_RETRY_LOCK:
        return _retry_import_job(db, job_id)


def _retry_import_job(db: Session, job_id: str) -> PaperImportJob:
    job = get_import_job(db, job_id)
    failed_items = [item for item in job.items if item.status == "failed"]
    if not failed_items:
        raise AppError(
            "任务中没有可重试的失败文件。",
            code="NO_FAILED_IMPORT_ITEMS",
            status_code=409,
        )
    retryable_items = [
        item for item in failed_items if _is_retryable_item(db, job, item)
    ]
    if not retryable_items:
        raise AppError(
            "没有可重试的失败文件；暂存文件可能已不存在。",
            code="NO_RETRYABLE_IMPORT_ITEMS",
            status_code=409,
        )
    try:
        for item in retryable_items:
            _queue_failed_item(item)
        _refresh_job_state(db, job.id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(job)
    return job


def retry_import_item(db: Session, job_id: str, item_id: str) -> PaperImportJob:
    with _IMPORT_RETRY_LOCK:
        return _retry_import_item(db, job_id, item_id)


def _retry_import_item(db: Session, job_id: str, item_id: str) -> PaperImportJob:
    job = get_import_job(db, job_id)
    item = (
        db.query(PaperImportItem)
        .filter(
            PaperImportItem.id == item_id,
            PaperImportItem.job_id == job_id,
        )
        .first()
    )
    if item is None:
        raise AppError("导入任务文件不存在。", code="IMPORT_ITEM_NOT_FOUND", status_code=404)
    if item.status != "failed":
        raise AppError(
            "只能重试失败的任务文件。",
            code="IMPORT_ITEM_NOT_FAILED",
            status_code=409,
        )
    if not _is_retryable_item(db, job, item):
        raise AppError(
            f"重试文件已不存在：{item.original_filename}",
            code="IMPORT_FILE_MISSING",
            status_code=409,
        )
    try:
        _queue_failed_item(item)
        _refresh_job_state(db, job.id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(job)
    return job


def _is_retryable_item(
    db: Session,
    job: PaperImportJob,
    item: PaperImportItem,
) -> bool:
    if job.kind == "index_rebuild":
        if item.paper_id is None:
            return True
        return (
            item.paper_id is not None
            and db.query(Paper.id).filter(Paper.id == item.paper_id).first() is not None
        )
    return _safe_staged_file_exists(item.staged_path)


def _safe_staged_file_exists(staged_path: str | None) -> bool:
    if not staged_path:
        return False
    staging_root = (get_settings().upload_path / IMPORT_DIRECTORY).resolve()
    try:
        resolved_path = Path(staged_path).resolve()
        resolved_path.relative_to(staging_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved_path.is_file()


def _queue_failed_item(item: PaperImportItem) -> None:
    item.status = "queued"
    item.current_stage = "queued"
    item.progress = 0
    item.error_message = None


def cleanup_expired_import_files(
    session_factory: Callable[[], Session],
    upload_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    import_root = (
        upload_dir or (get_settings().upload_path / IMPORT_DIRECTORY)
    ).resolve()
    cutoff = (now or datetime.utcnow()) - timedelta(days=FAILED_IMPORT_RETENTION_DAYS)
    with _IMPORT_RETRY_LOCK:
        with session_factory() as db:
            try:
                expired_items = (
                    db.query(PaperImportItem)
                    .filter(
                        PaperImportItem.status == "failed",
                        PaperImportItem.staged_path.isnot(None),
                        PaperImportItem.updated_at < cutoff,
                    )
                    .all()
                )
                for item in expired_items:
                    staged_path = Path(item.staged_path)
                    try:
                        resolved_path = staged_path.resolve()
                        resolved_path.relative_to(import_root)
                    except (OSError, ValueError):
                        logger.error(
                            "Refusing to clean expired import file outside staging directory: "
                            "item_id=%s path=%s",
                            item.id,
                            staged_path,
                        )
                        continue
                    try:
                        resolved_path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception(
                            "Could not remove expired import file item_id=%s path=%s",
                            item.id,
                            resolved_path,
                        )
                        continue
                    item.staged_path = None
                    expiration_message = (
                        f"暂存文件已超过 {FAILED_IMPORT_RETENTION_DAYS} 天保留期，无法重试。"
                    )
                    item.error_message = (
                        f"{item.error_message}\n{expiration_message}"
                        if item.error_message
                        else expiration_message
                    )[:2000]
                referenced_paths: set[Path] = set()
                for (path,) in (
                    db.query(PaperImportItem.staged_path)
                    .filter(PaperImportItem.staged_path.isnot(None))
                    .all()
                ):
                    try:
                        referenced_paths.add(Path(path).resolve())
                    except OSError:
                        logger.exception("Could not resolve import staging path %s", path)
                if import_root.is_dir():
                    for candidate in import_root.glob("*.pdf"):
                        try:
                            resolved_path = candidate.resolve()
                            resolved_path.relative_to(import_root)
                            modified_at = datetime.fromtimestamp(
                                candidate.stat().st_mtime,
                                timezone.utc,
                            ).replace(tzinfo=None)
                        except (OSError, ValueError):
                            logger.exception(
                                "Could not inspect orphaned import staging file %s",
                                candidate,
                            )
                            continue
                        if resolved_path in referenced_paths or modified_at >= cutoff:
                            continue
                        try:
                            resolved_path.unlink(missing_ok=True)
                        except OSError:
                            logger.exception(
                                "Could not remove orphaned import staging file %s",
                                resolved_path,
                            )
                db.commit()
            except Exception:
                db.rollback()
                raise


def recover_interrupted_jobs(
    session_factory: Callable[[], Session],
) -> None:
    """Recover interrupted items and reconcile nonterminal job summaries."""
    with session_factory() as db:
        try:
            affected_job_ids: set[str] = set()
            now = datetime.utcnow()
            interrupted = (
                db.query(PaperImportItem)
                .filter(
                    PaperImportItem.status == "running",
                    or_(
                        PaperImportItem.lease_expires_at.is_(None),
                        PaperImportItem.lease_expires_at <= now,
                    ),
                )
                .all()
            )
            for item in interrupted:
                job = db.query(PaperImportJob).filter(PaperImportJob.id == item.job_id).first()
                if job is not None and job.kind == "index_rebuild":
                    paper_exists = (
                        item.paper_id is None
                        or db.query(Paper.id).filter(Paper.id == item.paper_id).first() is not None
                    )
                    if paper_exists:
                        item.status = "queued"
                        item.current_stage = "queued"
                        item.progress = 0
                        item.error_message = None
                        item.lease_owner = None
                        item.lease_expires_at = None
                        affected_job_ids.add(item.job_id)
                        continue
                if item.staged_path and Path(item.staged_path).is_file():
                    item.status = "queued"
                    item.current_stage = "queued"
                    item.progress = 0
                    item.error_message = None
                    item.lease_owner = None
                    item.lease_expires_at = None
                else:
                    item.status = "failed"
                    item.current_stage = "failed"
                    item.parse_status = "failed"
                    item.error_message = "服务重启后未找到暂存 PDF 文件。"
                    item.lease_owner = None
                    item.lease_expires_at = None
                affected_job_ids.add(item.job_id)

            nonterminal_job_ids = (
                db.query(PaperImportJob.id)
                .filter(PaperImportJob.status.in_(("queued", "running")))
                .all()
            )
            affected_job_ids.update(job_id for (job_id,) in nonterminal_job_ids)
            for job_id in affected_job_ids:
                _refresh_job_state(db, job_id)
            db.commit()
        except Exception:
            db.rollback()
            raise


def run_import_worker(
    session_factory: Callable[[], Session],
    stop_event: threading.Event,
) -> None:
    next_cleanup_at = 0.0
    worker_id = str(uuid4())
    while not stop_event.is_set():
        if time.monotonic() >= next_cleanup_at:
            next_cleanup_at = time.monotonic() + 24 * 60 * 60
            try:
                cleanup_expired_import_files(session_factory)
            except Exception:
                logger.exception("Could not clean expired import staging files")
        try:
            lease = _claim_next_item(session_factory, worker_id)
            if lease is None:
                stop_event.wait(0.25)
                continue
            heartbeat_stop = threading.Event()
            lease_lost = threading.Event()
            heartbeat = threading.Thread(
                target=_heartbeat_worker,
                args=(session_factory, lease, heartbeat_stop, lease_lost),
                name=f"import-heartbeat-{lease.item_id}",
                daemon=True,
            )
            heartbeat.start()
            try:
                _process_item(session_factory, lease.item_id, lease, lease_lost)
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=WORKER_HEARTBEAT_SECONDS + 1)
        except Exception:
            logger.exception("Import worker encountered an unexpected failure")
            stop_event.wait(1)


def _claim_next_item(
    session_factory: Callable[[], Session],
    worker_id: str,
) -> WorkerLease | None:
    now = datetime.utcnow()
    claimable = or_(
        PaperImportItem.status == "queued",
        and_(
            PaperImportItem.status == "running",
            or_(
                PaperImportItem.lease_expires_at.is_(None),
                PaperImportItem.lease_expires_at <= now,
            ),
        ),
    )
    with session_factory() as db:
        candidate = (
            db.query(PaperImportItem.id, PaperImportItem.job_id)
            .filter(claimable)
            .order_by(PaperImportItem.created_at.asc())
            .first()
        )
        if candidate is None:
            return None
        result = db.execute(
            update(PaperImportItem)
            .where(
                PaperImportItem.id == candidate.id,
                claimable,
            )
            .values(
                status="running",
                current_stage="parsing",
                progress=5,
                attempts=PaperImportItem.attempts + 1,
                error_message=None,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=WORKER_LEASE_SECONDS),
                lease_generation=PaperImportItem.lease_generation + 1,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        db.query(PaperImportJob).filter(
            PaperImportJob.id == candidate.job_id
        ).update({PaperImportJob.status: "running"})
        generation = (
            db.query(PaperImportItem.lease_generation)
            .filter(PaperImportItem.id == candidate.id)
            .scalar()
        )
        db.commit()
        return WorkerLease(candidate.id, worker_id, generation)


def _heartbeat_worker(
    session_factory: Callable[[], Session],
    lease: WorkerLease,
    stop_event: threading.Event,
    lease_lost: threading.Event,
) -> None:
    while not stop_event.wait(WORKER_HEARTBEAT_SECONDS):
        renewed = False
        busy_deferred = False
        for busy_attempt in range(LEASE_HEARTBEAT_BUSY_ATTEMPTS):
            now = datetime.utcnow()
            try:
                with session_factory() as db:
                    result = db.execute(
                        update(PaperImportItem)
                        .where(
                            PaperImportItem.id == lease.item_id,
                            PaperImportItem.status == "running",
                            PaperImportItem.lease_owner == lease.owner,
                            PaperImportItem.lease_generation == lease.generation,
                            PaperImportItem.lease_expires_at > now,
                        )
                        .values(
                            lease_expires_at=(
                                now + timedelta(seconds=WORKER_LEASE_SECONDS)
                            ),
                        )
                    )
                    db.commit()
                    if result.rowcount != 1:
                        break
                    renewed = True
                    break
            except OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    lease_lost.set()
                    logger.exception(
                        "Could not renew import worker lease item_id=%s",
                        lease.item_id,
                    )
                    return
                if busy_attempt + 1 >= LEASE_HEARTBEAT_BUSY_ATTEMPTS:
                    busy_deferred = True
                    break
                delay = LEASE_FENCE_RETRY_BASE_SECONDS * (
                    2 ** busy_attempt
                )
                if stop_event.wait(delay):
                    return
            except Exception:
                lease_lost.set()
                logger.exception(
                    "Could not renew import worker lease item_id=%s",
                    lease.item_id,
                )
                return

        if renewed:
            continue
        if busy_deferred:
            logger.warning(
                "Lease heartbeat deferred by active SQLite write transaction "
                "item_id=%s generation=%s",
                lease.item_id,
                lease.generation,
            )
            continue

        lease_lost.set()
        logger.warning(
            "Import worker lease lost item_id=%s generation=%s",
            lease.item_id,
            lease.generation,
        )
        return


def _record_fence_busy_deferral(
    session_factory: Callable[[], Session],
    item_id: str,
    lease: WorkerLease,
    message: str,
    *,
    stage: str = "fence_busy",
) -> None:
    deadline = time.monotonic() + LEASE_FENCE_MAX_WAIT_SECONDS
    for attempt in range(LEASE_FENCE_MAX_ATTEMPTS):
        try:
            with session_factory() as db:
                result = db.execute(
                    update(PaperImportItem)
                    .where(
                        PaperImportItem.id == item_id,
                        PaperImportItem.status == "running",
                        PaperImportItem.lease_owner == lease.owner,
                        PaperImportItem.lease_generation == lease.generation,
                    )
                    .values(
                        current_stage=stage,
                        error_message=message[:2000],
                    )
                )
                db.commit()
                if result.rowcount != 1:
                    logger.warning(
                        "Could not record fence-busy deferral because lease changed "
                        "item_id=%s generation=%s",
                        item_id,
                        lease.generation,
                    )
                return
        except OperationalError as exc:
            text_value = str(exc).lower()
            if "locked" not in text_value and "busy" not in text_value:
                logger.exception(
                    "Could not persist fence-busy deferral item_id=%s",
                    item_id,
                )
                return
            remaining = deadline - time.monotonic()
            if attempt + 1 >= LEASE_FENCE_MAX_ATTEMPTS or remaining <= 0:
                logger.warning(
                    "Deferred recording of fence-busy state item_id=%s "
                    "generation=%s because SQLite remains busy",
                    item_id,
                    lease.generation,
                )
                return
            time.sleep(
                min(
                    LEASE_FENCE_RETRY_BASE_SECONDS * (2 ** attempt),
                    remaining,
                )
            )
        except Exception:
            logger.exception(
                "Could not persist fence-busy deferral item_id=%s generation=%s",
                item_id,
                lease.generation,
            )
            return


def _process_item(
    session_factory: Callable[[], Session],
    item_id: str,
    lease: WorkerLease,
    lease_lost: threading.Event,
) -> None:
    rebuild_paper_id: int | None = None
    rebuild_item_id: str | None = None
    with session_factory() as db:
        item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).first()
        if item is None:
            return
        _assert_worker_lease(db, lease, lease_lost)
        job = db.query(PaperImportJob).filter(PaperImportJob.id == item.job_id).first()
        if job is not None and job.kind == "index_rebuild":
            rebuild_paper_id = item.paper_id
            if rebuild_paper_id is None:
                rebuild_item_id = item.id
            else:
                rebuild_item_id = None
        else:
            if item.staged_path is None:
                _fail_item(db, item, "导入任务缺少暂存文件路径。", lease)
                return
            staged_path = Path(item.staged_path)
            upload_root = (get_settings().upload_path / IMPORT_DIRECTORY).resolve()
            try:
                resolved_path = staged_path.resolve()
                resolved_path.relative_to(upload_root)
            except (OSError, ValueError):
                _fail_item(db, item, "暂存文件路径不在允许的导入目录中。", lease)
                return
            filename = item.original_filename

    if rebuild_paper_id is not None:
        _process_index_item(
            session_factory,
            item_id,
            rebuild_paper_id,
            lease,
            lease_lost,
        )
        return
    if rebuild_item_id is not None:
        _process_index_reconciliation(
            session_factory,
            rebuild_item_id,
            lease,
            lease_lost,
        )
        return

    try:
        if not resolved_path.is_file():
            raise FileNotFoundError(f"暂存文件不存在：{filename}")
        content = resolved_path.read_bytes()
        if len(content) > get_settings().max_upload_bytes:
            raise ValueError("文件超过单篇上传大小限制。")
        file_hash = hashlib.sha256(content).hexdigest()

        with session_factory() as db:
            paper = db.query(Paper).filter(Paper.file_hash == file_hash).first()
            item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).one()
            if paper is not None and item.paper_id != paper.id:
                raise AppError(
                    f"检测到完全相同的重复文件，库中已存在《{paper.original_filename}》。",
                    code="DUPLICATE_PAPER",
                    status_code=409,
                )
            def link_paper(created_paper: Paper) -> None:
                _fence_worker_lease(db, lease, lease_lost)
                item.paper_id = created_paper.id
                item.parse_status = created_paper.parse_status
                item.index_status = created_paper.index_status

            def on_parse_progress(completed: int, total: int) -> None:
                parsed_progress = 5 + int(45 * completed / max(total, 1))
                with session_factory() as progress_db:
                    _update_item(
                        progress_db,
                        item_id,
                        current_stage="parsing",
                        parse_status=(
                            "parsed"
                            if completed >= total
                            else "pending"
                        ),
                        progress=parsed_progress,
                        lease=lease,
                        defer_on_busy=True,
                    )

            paper = create_or_resume_paper(
                db=db,
                filename=filename,
                content=content,
                content_type="application/pdf",
                on_saved=link_paper,
                lease_guard=lambda: _fence_worker_lease(
                    db,
                    lease,
                    lease_lost,
                ),
                commit_lease_guard=lambda: _fence_worker_lease(
                    db,
                    lease,
                    lease_lost,
                    retry_busy=False,
                ),
                worker_owner=lease.owner,
                worker_generation=lease.generation,
                progress_callback=on_parse_progress,
            )
            paper_id = paper.id
            parse_status = paper.parse_status
            index_status = paper.index_status
            completeness = get_paper_parse_completeness(db, paper_id)
            if not completeness.complete:
                raise AppError(
                    completeness.failure_message(),
                    code="PDF_PARSE_INCOMPLETE",
                    status_code=409,
                    details={
                        "paper_id": paper_id,
                        "expected_page_count": completeness.expected_page_count,
                        "recorded_page_count": completeness.recorded_page_count,
                        "completed_page_count": completeness.completed_page_count,
                        "failed_page_count": completeness.failed_page_count,
                        "processing_page_count": completeness.processing_page_count,
                        "pending_page_count": completeness.pending_page_count,
                        "missing_page_count": completeness.missing_page_count,
                        "incomplete_page_numbers": list(
                            completeness.incomplete_page_numbers
                        ),
                    },
                )
            _update_item(
                db,
                item_id,
                paper_id=paper_id,
                parse_status=parse_status,
                index_status=index_status,
                current_stage="indexing",
                progress=50,
                lease=lease,
            )

            def on_index_progress(completed: int, total: int) -> None:
                index_progress = 50 + int(49 * completed / max(total, 1))
                with session_factory() as progress_db:
                    _update_item(
                        progress_db,
                        item_id,
                        current_stage="indexing",
                        index_status="indexing",
                        progress=index_progress,
                        lease=lease,
                        defer_on_busy=True,
                    )

            try:
                indexed = index_paper(
                    db=db,
                    paper_id=paper_id,
                    progress_callback=on_index_progress,
                )
                if indexed.get("status") != "indexed":
                    raise RuntimeError(indexed.get("message", "向量索引未完成。"))
            except Exception:
                db.rollback()
                raise

            _update_item(
                db,
                item_id,
                paper_id=paper_id,
                parse_status=parse_status,
                index_status="indexed",
                current_stage="completed",
                progress=100,
                status="succeeded",
                error_message=None,
                lease=lease,
            )
        resolved_path.unlink(missing_ok=True)
    except LeaseFenceBusy as exc:
        logger.warning(
            "Import item deferred because SQLite remained busy item_id=%s "
            "generation=%s",
            item_id,
            lease.generation,
        )
        _record_fence_busy_deferral(
            session_factory,
            item_id,
            lease,
            str(exc),
        )
        return
    except TaskStatusBusy as exc:
        logger.warning(
            "Import item deferred because task status update stayed busy "
            "item_id=%s generation=%s",
            item_id,
            lease.generation,
        )
        _record_fence_busy_deferral(
            session_factory,
            item_id,
            lease,
            str(exc),
            stage="status_busy",
        )
        return
    except WorkerLeaseLost:
        logger.warning(
            "Import worker stopped after losing lease item_id=%s generation=%s",
            item_id,
            lease.generation,
        )
        return
    except Exception as exc:
        logger.exception("Import item failed item_id=%s filename=%s", item_id, filename)
        with session_factory() as db:
            item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).first()
            if item is not None:
                paper = (
                    db.query(Paper)
                    .filter(Paper.id == item.paper_id)
                    .first()
                    if item.paper_id is not None
                    else None
                )
                if paper is not None:
                    item.parse_status = paper.parse_status
                    item.index_status = paper.index_status
                _fail_item(db, item, str(exc), lease)


def _process_index_reconciliation(
    session_factory: Callable[[], Session],
    item_id: str,
    lease: WorkerLease,
    lease_lost: threading.Event,
) -> None:
    try:
        with session_factory() as db:
            _assert_worker_lease(db, lease, lease_lost)
            result = reconcile_index_consistency(db, paper_ids=[])
            _update_item(
                db,
                item_id,
                status="succeeded",
                current_stage="completed",
                index_status="indexed",
                progress=100,
                error_message=None,
                lease=lease,
            )
            logger.info("Index reconciliation completed: %s", result)
        _reconcile_completed_rebuild(session_factory, item_id)
    except TaskStatusBusy as exc:
        logger.warning(
            "Index reconciliation deferred because task status update "
            "stayed busy item_id=%s",
            item_id,
        )
        _record_fence_busy_deferral(
            session_factory,
            item_id,
            lease,
            str(exc),
            stage="status_busy",
        )
        return
    except WorkerLeaseLost:
        logger.warning(
            "Index reconciliation stopped after losing lease item_id=%s",
            item_id,
        )
        return
    except Exception as exc:
        logger.exception("Index reconciliation failed item_id=%s", item_id)
        with session_factory() as db:
            item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).first()
            if item is not None:
                _fail_item(db, item, str(exc), lease)


def _reconcile_completed_rebuild(
    session_factory: Callable[[], Session],
    item_id: str,
) -> None:
    with session_factory() as db:
        item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).first()
        if item is None:
            return
        job = db.query(PaperImportJob).filter(PaperImportJob.id == item.job_id).first()
        if job is None or job.kind != "index_rebuild":
            return
        unfinished = (
            db.query(PaperImportItem.id)
            .filter(
                PaperImportItem.job_id == job.id,
                PaperImportItem.status.in_(("queued", "running", "failed")),
            )
            .first()
        )
        if unfinished is not None:
            return
        try:
            target_paper_ids = [
                int(paper_id)
                for (paper_id,) in (
                    db.query(PaperImportItem.paper_id)
                    .filter(
                        PaperImportItem.job_id == job.id,
                        PaperImportItem.paper_id.isnot(None),
                    )
                    .all()
                )
            ]
            audit = reconcile_index_consistency(
                db,
                paper_ids=target_paper_ids,
                raise_on_inconsistent=False,
            )
            if audit["consistent"]:
                job.status = "completed"
                db.query(PaperIndexRepairLock).filter(
                    PaperIndexRepairLock.job_id == job.id
                ).delete(synchronize_session=False)
                db.commit()
                return

            affected_paper_ids = {
                int(paper_id)
                for paper_id in audit["affected_paper_ids"]
            }
            affected_paper_ids.update(
                int(paper_id)
                for paper_id in audit.get("parse_affected_paper_ids", [])
            )
            paper_items = {
                int(item.paper_id): item
                for item in (
                    db.query(PaperImportItem)
                    .filter(
                        PaperImportItem.job_id == job.id,
                        PaperImportItem.paper_id.isnot(None),
                    )
                    .all()
                )
            }
            error_summary = (
                "Final index audit did not converge: "
                f"missing={audit['missing_vector_count']} "
                f"stale={audit['stale_vector_count']} "
                f"orphaned={audit['orphan_vector_count']} "
                f"unverifiable={audit['unverifiable_vector_count']} "
                f"parse_incomplete={audit.get('parse_incomplete_page_count', 0)}."
            )
            for paper_id in sorted(affected_paper_ids):
                item = paper_items.get(paper_id)
                if item is None:
                    paper = db.query(Paper).filter(Paper.id == paper_id).first()
                    if paper is None:
                        continue
                    item = PaperImportItem(
                        job_id=job.id,
                        paper_id=paper.id,
                        original_filename=paper.original_filename,
                        staged_path=None,
                        file_size=paper.file_size,
                        status="failed",
                        parse_status=paper.parse_status,
                        index_status="failed",
                        current_stage="failed",
                        error_message=(
                            f"{error_summary} Affected paper_id={paper_id} "
                            "had no task item; a repair item was added."
                        )[:2000],
                    )
                    db.add(item)
                    paper_items[paper_id] = item
                else:
                    item.status = "failed"
                    item.current_stage = "failed"
                    item.index_status = "failed"
                    item.error_message = error_summary[:2000]
                paper = db.query(Paper).filter(Paper.id == paper_id).first()
                if paper is not None:
                    paper.index_status = "failed"

            if (
                audit["orphan_vector_count"]
                or (
                    audit["unverifiable_vector_count"]
                    and not affected_paper_ids
                )
            ):
                db.add(
                    PaperImportItem(
                        job_id=job.id,
                        paper_id=None,
                        original_filename="索引全局最终复查",
                        staged_path=None,
                        file_size=0,
                        status="failed",
                        current_stage="failed",
                        index_status="failed",
                        error_message=error_summary[:2000],
                    )
                )
            db.flush()
            _refresh_job_state(db, job.id)
            db.commit()
        except Exception as exc:
            logger.exception("Index reconciliation failed for job_id=%s", job.id)
            db.add(
                PaperImportItem(
                    job_id=job.id,
                    paper_id=None,
                    original_filename="索引最终复查",
                    staged_path=None,
                    file_size=0,
                    status="failed",
                    current_stage="failed",
                    index_status="failed",
                    error_message=str(exc)[:2000],
                )
            )
            db.flush()
            _refresh_job_state(db, job.id)
            db.commit()


def _process_index_item(
    session_factory: Callable[[], Session],
    item_id: str,
    paper_id: int,
    lease: WorkerLease,
    lease_lost: threading.Event,
) -> None:
    try:
        with session_factory() as db:
            paper = db.query(Paper).filter(Paper.id == paper_id).first()
            item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).first()
            if paper is None or item is None:
                raise ValueError(f"论文或索引任务不存在：paper_id={paper_id}")
            _assert_worker_lease(db, lease, lease_lost)

            def on_index_progress(completed: int, total: int) -> None:
                progress = int(99 * completed / max(total, 1))
                with session_factory() as progress_db:
                    _update_item(
                        progress_db,
                        item_id,
                        current_stage="indexing",
                        index_status="indexing",
                        progress=progress,
                        lease=lease,
                        defer_on_busy=True,
                    )

            result = index_paper(
                db=db,
                paper_id=paper_id,
                force=False,
                progress_callback=on_index_progress,
            )
            verification = audit_paper_index_consistency(
                db=db,
                paper_id=paper_id,
            )
            if not verification["consistent"]:
                paper.index_status = "failed"
                db.commit()
                raise RuntimeError(
                    "Indexed paper did not converge: "
                    f"missing={verification['missing_vector_count']} "
                    f"stale={verification['stale_vector_count']} "
                    f"unverifiable={verification['unverifiable_vector_count']}."
                )
            if result.get("status") != "indexed":
                raise RuntimeError(result.get("message", "向量索引未完成。"))
            _update_item(
                db,
                item_id,
                status="succeeded",
                current_stage="completed",
                index_status="indexed",
                progress=100,
                error_message=None,
                lease=lease,
            )
        _reconcile_completed_rebuild(session_factory, item_id)
    except TaskStatusBusy as exc:
        logger.warning(
            "Index rebuild deferred because task status update stayed busy "
            "item_id=%s paper_id=%s",
            item_id,
            paper_id,
        )
        _record_fence_busy_deferral(
            session_factory,
            item_id,
            lease,
            str(exc),
            stage="status_busy",
        )
        return
    except WorkerLeaseLost:
        logger.warning(
            "Index rebuild stopped after losing lease item_id=%s paper_id=%s",
            item_id,
            paper_id,
        )
        return
    except Exception as exc:
        logger.exception("Index rebuild failed item_id=%s paper_id=%s", item_id, paper_id)
        with session_factory() as db:
            item = db.query(PaperImportItem).filter(PaperImportItem.id == item_id).first()
            if item is not None:
                paper = db.query(Paper).filter(Paper.id == paper_id).first()
                if paper is not None:
                    item.index_status = paper.index_status
                _fail_item(db, item, str(exc), lease)


def _assert_worker_lease(
    db: Session,
    lease: WorkerLease,
    lease_lost: threading.Event | None = None,
) -> None:
    if lease_lost is not None and lease_lost.is_set():
        raise WorkerLeaseLost(f"Worker lease lost for item_id={lease.item_id}")
    now = datetime.utcnow()
    owned = (
        db.query(PaperImportItem.id)
        .filter(
            PaperImportItem.id == lease.item_id,
            PaperImportItem.status == "running",
            PaperImportItem.lease_owner == lease.owner,
            PaperImportItem.lease_generation == lease.generation,
            PaperImportItem.lease_expires_at > now,
        )
        .first()
    )
    if owned is None:
        raise WorkerLeaseLost(f"Worker lease expired for item_id={lease.item_id}")


def _fence_worker_lease(
    db: Session,
    lease: WorkerLease,
    lease_lost: threading.Event | None = None,
    *,
    retry_busy: bool = True,
    max_attempts: int = LEASE_FENCE_MAX_ATTEMPTS,
    retry_base_seconds: float = LEASE_FENCE_RETRY_BASE_SECONDS,
    max_wait_seconds: float = LEASE_FENCE_MAX_WAIT_SECONDS,
) -> None:
    """Acquire SQLite's write lock and atomically validate the worker lease."""
    if lease_lost is not None and lease_lost.is_set():
        raise WorkerLeaseLost(f"Worker lease lost for item_id={lease.item_id}")
    deadline = time.monotonic() + max(
        max_wait_seconds,
        retry_base_seconds,
    )
    attempt = 0
    while True:
        attempt += 1
        if lease_lost is not None and lease_lost.is_set():
            raise WorkerLeaseLost(f"Worker lease lost for item_id={lease.item_id}")
        now = datetime.utcnow()
        try:
            result = db.execute(
                update(PaperImportItem)
                .where(
                    PaperImportItem.id == lease.item_id,
                    PaperImportItem.status == "running",
                    PaperImportItem.lease_owner == lease.owner,
                    PaperImportItem.lease_generation == lease.generation,
                    PaperImportItem.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=PaperImportItem.lease_expires_at,
                )
            )
        except OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if not retry_busy:
                raise LeaseFenceBusy(
                    "SQLite is busy during final page commit fencing: "
                    f"item_id={lease.item_id} generation={lease.generation}"
                ) from exc
            db.rollback()
            delay = retry_base_seconds * (2 ** min(attempt - 1, 4))
            remaining = deadline - time.monotonic()
            if attempt >= max_attempts or remaining <= 0:
                raise LeaseFenceBusy(
                    "SQLite remained busy while validating worker fencing: "
                    f"item_id={lease.item_id} generation={lease.generation}"
                ) from exc
            if lease_lost is not None:
                if lease_lost.wait(min(delay, remaining)):
                    raise WorkerLeaseLost(
                        f"Worker lease lost for item_id={lease.item_id}"
                    )
            else:
                time.sleep(min(delay, remaining))
            continue

        if result.rowcount != 1:
            raise WorkerLeaseLost(
                "Worker lease was lost before the page transaction: "
                f"item_id={lease.item_id} generation={lease.generation}"
            )
        return


def _update_item(
    db: Session,
    item_id: str,
    lease: WorkerLease | None = None,
    *,
    defer_on_busy: bool = False,
    max_attempts: int = TASK_STATUS_BUSY_MAX_ATTEMPTS,
    retry_base_seconds: float = LEASE_FENCE_RETRY_BASE_SECONDS,
    max_wait_seconds: float = TASK_STATUS_BUSY_MAX_WAIT_SECONDS,
    **values,
) -> bool:
    deadline = time.monotonic() + max(
        max_wait_seconds,
        retry_base_seconds,
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            item = (
                db.query(PaperImportItem)
                .filter(PaperImportItem.id == item_id)
                .first()
            )
            if item is None:
                return True
            job_id = item.job_id
            query = db.query(PaperImportItem).filter(
                PaperImportItem.id == item_id
            )
            if lease is not None:
                query = query.filter(
                    PaperImportItem.status == "running",
                    PaperImportItem.lease_owner == lease.owner,
                    PaperImportItem.lease_generation == lease.generation,
                    PaperImportItem.lease_expires_at > datetime.utcnow(),
                )
            update_values = dict(values)
            if update_values.get("status") in {"succeeded", "failed"}:
                update_values["lease_owner"] = None
                update_values["lease_expires_at"] = None
            result = query.update(
                update_values,
                synchronize_session=False,
            )
            if lease is not None and result != 1:
                raise WorkerLeaseLost(
                    f"Worker lease lost for item_id={item_id}"
                )
            _refresh_job_state(db, job_id)
            db.commit()
            return True
        except OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                db.rollback()
                raise
            db.rollback()
            delay = retry_base_seconds * (2 ** min(attempt - 1, 4))
            remaining = deadline - time.monotonic()
            if attempt >= max_attempts or remaining <= 0:
                if defer_on_busy:
                    logger.warning(
                        "Task progress update skipped after SQLite stayed busy "
                        "item_id=%s generation=%s",
                        item_id,
                        lease.generation if lease is not None else None,
                    )
                    return False
                raise TaskStatusBusy(
                    "SQLite remained busy while updating task status: "
                    f"item_id={item_id}"
                ) from exc
            time.sleep(min(delay, remaining))
        except Exception:
            db.rollback()
            raise


def _fail_item(
    db: Session,
    item: PaperImportItem,
    message: str,
    lease: WorkerLease | None = None,
) -> None:
    try:
        values = {
            "status": "failed",
            "current_stage": "failed",
            "error_message": message[:2000],
            "lease_owner": None,
            "lease_expires_at": None,
        }
        if item.current_stage == "parsing":
            values["parse_status"] = "failed"
        elif item.current_stage == "indexing" and item.index_status != "indexed":
            values["index_status"] = "failed"
        query = db.query(PaperImportItem).filter(PaperImportItem.id == item.id)
        if lease is not None:
            query = query.filter(
                PaperImportItem.status == "running",
                PaperImportItem.lease_owner == lease.owner,
                PaperImportItem.lease_generation == lease.generation,
                PaperImportItem.lease_expires_at > datetime.utcnow(),
            )
        result = query.update(values, synchronize_session=False)
        if lease is not None and result != 1:
            raise WorkerLeaseLost(f"Worker lease lost for item_id={item.id}")
        _refresh_job_state(db, item.job_id)
        db.commit()
    except Exception:
        db.rollback()
        raise


def _refresh_job_state(db: Session, job_id: str) -> None:
    db.flush()
    counts = (
        db.query(PaperImportItem.status, func.count(PaperImportItem.id))
        .filter(PaperImportItem.job_id == job_id)
        .group_by(PaperImportItem.status)
        .all()
    )
    by_status = dict(counts)
    job = db.query(PaperImportJob).filter(PaperImportJob.id == job_id).first()
    if job is None:
        return
    job.total_count = sum(by_status.values())
    job.completed_count = by_status.get("succeeded", 0)
    job.failed_count = by_status.get("failed", 0)
    if by_status.get("running", 0):
        job.status = "running"
    elif by_status.get("queued", 0):
        job.status = "queued"
    elif job.failed_count and job.completed_count:
        job.status = "partial"
    elif job.failed_count:
        job.status = "failed"
    elif job.kind == "index_rebuild":
        job.status = "running"
    else:
        job.status = "completed"

    if job.status in {"completed", "partial", "failed"}:
        db.query(PaperIndexRepairLock).filter(
            PaperIndexRepairLock.job_id == job.id
        ).delete(synchronize_session=False)
    elif job.kind == "index_rebuild":
        paper_ids = [
            int(paper_id)
            for (paper_id,) in (
                db.query(PaperImportItem.paper_id)
                .filter(
                    PaperImportItem.job_id == job.id,
                    PaperImportItem.paper_id.isnot(None),
                )
                .all()
            )
        ]
        include_reconciliation = (
            db.query(PaperImportItem.id)
            .filter(
                PaperImportItem.job_id == job.id,
                PaperImportItem.paper_id.is_(None),
            )
            .first()
            is not None
        )
        target_key = _index_repair_target_key(
            paper_ids,
            include_reconciliation,
        )
        lock = (
            db.query(PaperIndexRepairLock)
            .filter(PaperIndexRepairLock.target_key == target_key)
            .first()
        )
        if lock is None:
            db.add(
                PaperIndexRepairLock(
                    target_key=target_key,
                    job_id=job.id,
                )
            )
        elif lock.job_id != job.id:
            raise RuntimeError(
                "Another active index repair job owns the same target set: "
                f"job_id={lock.job_id}"
            )
