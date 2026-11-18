from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.discovery import DiscoveryEnvelope
from app.schemas.review import (
    LiteratureReviewRequest,
    LiteratureReviewResponse,
    LiteratureReviewSessionEnvelope,
)
from app.services.literature_review_service import generate_literature_review
from app.services.review_record_service import (
    create_review_record,
    get_latest_review_record,
    get_review_record,
    retry_review_record,
    run_review_record,
    serialize_review_record,
)


router = APIRouter(prefix="/reviews", tags=["reviews"])


def _session_factory(db: Session):
    return sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        future=True,
    )


@router.get(
    "/sessions/latest",
    response_model=LiteratureReviewSessionEnvelope,
)
def latest_review_session(db: Session = Depends(get_db)):
    return success(get_latest_review_record(db))


@router.post(
    "/sessions",
    status_code=202,
    response_model=DiscoveryEnvelope,
)
def create_review_session(
    request: LiteratureReviewRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    record = create_review_record(db, request)
    if record.status in {"queued", "running"}:
        background_tasks.add_task(
            run_review_record,
            record.id,
            _session_factory(db),
        )
    return success(serialize_review_record(record), status_code=202)


@router.get("/sessions/{review_id}", response_model=DiscoveryEnvelope)
def review_session(review_id: str, db: Session = Depends(get_db)):
    return success(get_review_record(db, review_id))


@router.post(
    "/sessions/{review_id}/retry",
    status_code=202,
    response_model=DiscoveryEnvelope,
)
def retry_review_session(
    review_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    record = retry_review_record(db, review_id)
    if record.status == "queued":
        background_tasks.add_task(
            run_review_record,
            record.id,
            _session_factory(db),
        )
    return success(serialize_review_record(record), status_code=202)


@router.post("", response_model=LiteratureReviewResponse)
def create_literature_review(
    request: LiteratureReviewRequest,
    db: Session = Depends(get_db),
):
    return success(
        generate_literature_review(
            db,
            paper_ids=request.paper_ids,
            topic=request.topic,
            include_future_work=request.include_future_work,
        )
    )
