from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.research_agent import ResearchAgentRunCreate
from app.services.research_agent.service import (
    cancel_research_run,
    create_research_run,
    get_report,
    get_run_or_404,
    serialize_run,
    start_research_run_from_session,
)


router = APIRouter(
    prefix="/research-agent",
    tags=["research-agent"],
)


@router.post("/runs", status_code=202)
def create_run(
    request: ResearchAgentRunCreate,
    db: Session = Depends(get_db),
):
    run = create_research_run(
        db,
        goal=request.goal,
        budget=request.budget,
    )
    start_research_run_from_session(db, run.id)
    db.refresh(run)
    return success(serialize_run(db, run), status_code=202)


@router.get("/runs/{run_id}")
def read_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    run = get_run_or_404(db, run_id)
    return success(serialize_run(db, run))


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    run = cancel_research_run(db, run_id)
    return success(serialize_run(db, run))


@router.get("/runs/{run_id}/report")
def read_report(
    run_id: str,
    db: Session = Depends(get_db),
):
    return success(get_report(db, run_id))
