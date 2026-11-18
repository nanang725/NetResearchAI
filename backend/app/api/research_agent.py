from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.research_agent import (
    ResearchAgentPlanUpdate,
    ResearchAgentReportEnvelope,
    ResearchAgentRunEnvelope,
    ResearchAgentRunCreate,
    ResearchAgentToolCatalogEnvelope,
)
from app.services.research_agent.service import (
    cancel_research_run,
    confirm_research_plan,
    create_research_plan,
    create_research_run,
    get_report,
    get_run_or_404,
    serialize_run,
    serialize_tool_catalog,
    start_research_run_from_session,
    update_research_plan,
)


router = APIRouter(
    prefix="/research-agent",
    tags=["research-agent"],
)


@router.get(
    "/tools",
    response_model=ResearchAgentToolCatalogEnvelope,
)
def read_tool_catalog():
    return success(serialize_tool_catalog())


@router.post(
    "/plans",
    status_code=201,
    response_model=ResearchAgentRunEnvelope,
)
def create_plan(
    request: ResearchAgentRunCreate,
    db: Session = Depends(get_db),
):
    run = create_research_plan(
        db,
        goal=request.goal,
        budget=request.budget,
    )
    return success(serialize_run(db, run), status_code=201)


@router.get(
    "/plans/{plan_id}",
    response_model=ResearchAgentRunEnvelope,
)
def read_plan(
    plan_id: str,
    db: Session = Depends(get_db),
):
    run = get_run_or_404(db, plan_id)
    return success(serialize_run(db, run))


@router.put(
    "/plans/{plan_id}",
    response_model=ResearchAgentRunEnvelope,
)
def update_plan(
    plan_id: str,
    request: ResearchAgentPlanUpdate,
    db: Session = Depends(get_db),
):
    run = update_research_plan(db, plan_id, request)
    return success(serialize_run(db, run))


@router.post(
    "/plans/{plan_id}/confirm",
    status_code=202,
    response_model=ResearchAgentRunEnvelope,
)
def confirm_plan(
    plan_id: str,
    db: Session = Depends(get_db),
):
    run = confirm_research_plan(db, plan_id)
    return success(serialize_run(db, run), status_code=202)


@router.post(
    "/runs",
    status_code=202,
    response_model=ResearchAgentRunEnvelope,
)
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


@router.get(
    "/runs/{run_id}",
    response_model=ResearchAgentRunEnvelope,
)
def read_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    run = get_run_or_404(db, run_id)
    return success(serialize_run(db, run))


@router.post(
    "/runs/{run_id}/cancel",
    response_model=ResearchAgentRunEnvelope,
)
def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    run = cancel_research_run(db, run_id)
    return success(serialize_run(db, run))


@router.get(
    "/runs/{run_id}/report",
    response_model=ResearchAgentReportEnvelope,
)
def read_report(
    run_id: str,
    db: Session = Depends(get_db),
):
    return success(get_report(db, run_id))
