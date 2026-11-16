from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.review import LiteratureReviewRequest, LiteratureReviewResponse
from app.services.literature_review_service import generate_literature_review


router = APIRouter(prefix="/reviews", tags=["reviews"])


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
