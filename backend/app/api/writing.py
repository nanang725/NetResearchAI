from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.writing import (
    AcademicOutlineRequest,
    AcademicReferencesRequest,
    AcademicTranslationRequest,
    AcademicWritingEnvelope,
)
from app.services.academic_writing_service import (
    generate_academic_outline,
    list_verifiable_references,
)
from app.services.translation_service import translate_academic_content


router = APIRouter(prefix="/writing", tags=["writing"])


@router.post("/outline", response_model=AcademicWritingEnvelope)
def create_academic_outline(
    request: AcademicOutlineRequest,
    db: Session = Depends(get_db),
):
    return success(
        generate_academic_outline(
            db,
            paper_id=request.paper_id,
            topic=request.topic,
        )
    )


@router.post("/references", response_model=AcademicWritingEnvelope)
def create_verifiable_references(
    request: AcademicReferencesRequest,
    db: Session = Depends(get_db),
):
    return success(
        list_verifiable_references(
            db,
            paper_ids=request.paper_ids,
        )
    )


@router.post("/translate", response_model=AcademicWritingEnvelope)
def translate_generated_academic_content(
    request: AcademicTranslationRequest,
):
    return success(
        translate_academic_content(
            text=request.text,
            direction=request.direction,
        )
    )
