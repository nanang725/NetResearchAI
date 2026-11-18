from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.review import LiteratureReviewRecord
from app.schemas.review import LiteratureReviewRequest
from app.services.literature_review_service import generate_literature_review


ACTIVE_REVIEW_STATUSES = {"queued", "running"}


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _get_record(db: Session, review_id: str) -> LiteratureReviewRecord:
    record = db.get(LiteratureReviewRecord, review_id)
    if record is None:
        raise AppError(
            "综述任务不存在。",
            code="LITERATURE_REVIEW_NOT_FOUND",
            status_code=404,
            details={"review_id": review_id},
        )
    return record


def serialize_review_record(record: LiteratureReviewRecord) -> dict[str, Any]:
    return {
        "review_id": record.id,
        "selected_paper_ids": _loads(record.selected_paper_ids_json, []),
        "topic": record.topic,
        "include_future_work": record.include_future_work,
        "status": record.status,
        "result": _loads(record.result_json, None),
        "error": record.error_message,
        "created_at": (
            record.created_at.isoformat() if record.created_at else None
        ),
        "started_at": (
            record.started_at.isoformat() if record.started_at else None
        ),
        "completed_at": (
            record.completed_at.isoformat() if record.completed_at else None
        ),
    }


def create_review_record(
    db: Session,
    request: LiteratureReviewRequest,
) -> LiteratureReviewRecord:
    active = (
        db.query(LiteratureReviewRecord)
        .filter(LiteratureReviewRecord.status.in_(ACTIVE_REVIEW_STATUSES))
        .order_by(LiteratureReviewRecord.created_at.desc())
        .first()
    )
    if (
        active is not None
        and _loads(active.selected_paper_ids_json, []) == request.paper_ids
        and active.topic == request.topic.strip()
        and active.include_future_work == request.include_future_work
    ):
        return active

    record = LiteratureReviewRecord(
        id=str(uuid4()),
        selected_paper_ids_json=_json_dump(request.paper_ids),
        topic=request.topic.strip(),
        include_future_work=request.include_future_work,
        status="queued",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_review_record(db: Session, review_id: str) -> dict[str, Any]:
    return serialize_review_record(_get_record(db, review_id))


def get_latest_review_record(db: Session) -> dict[str, Any] | None:
    record = (
        db.query(LiteratureReviewRecord)
        .order_by(LiteratureReviewRecord.created_at.desc())
        .first()
    )
    return serialize_review_record(record) if record is not None else None


def recover_interrupted_review_records(db: Session) -> int:
    records = (
        db.query(LiteratureReviewRecord)
        .filter(LiteratureReviewRecord.status.in_(ACTIVE_REVIEW_STATUSES))
        .all()
    )
    if not records:
        return 0
    now = datetime.utcnow()
    for record in records:
        record.status = "failed"
        record.error_message = (
            "服务在综述执行期间重启，任务状态已保留。"
            "请点击重试继续，系统不会伪造未完成结果。"
        )
        record.completed_at = now
    db.commit()
    return len(records)


def run_review_record(
    review_id: str,
    session_factory: Callable[[], Session],
) -> None:
    db = session_factory()
    try:
        record = _get_record(db, review_id)
        if record.status != "queued":
            return
        record.status = "running"
        record.started_at = record.started_at or datetime.utcnow()
        record.completed_at = None
        record.error_message = None
        db.commit()
        try:
            result = generate_literature_review(
                db,
                paper_ids=_loads(record.selected_paper_ids_json, []),
                topic=record.topic,
                include_future_work=record.include_future_work,
            )
            record = _get_record(db, review_id)
            record.result_json = _json_dump(result)
            record.status = "completed"
            record.completed_at = datetime.utcnow()
            record.error_message = None
            db.commit()
        except Exception as exc:
            db.rollback()
            record = _get_record(db, review_id)
            record.status = "failed"
            record.completed_at = datetime.utcnow()
            record.error_message = getattr(exc, "message", str(exc))[:4000]
            db.commit()
    finally:
        db.close()


def retry_review_record(db: Session, review_id: str) -> LiteratureReviewRecord:
    record = _get_record(db, review_id)
    if record.status in ACTIVE_REVIEW_STATUSES:
        return record
    record.status = "queued"
    record.error_message = None
    record.result_json = None
    record.started_at = None
    record.completed_at = None
    db.commit()
    db.refresh(record)
    return record
