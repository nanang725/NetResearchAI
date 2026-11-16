from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.responses import success
from app.models.paper import Paper
from app.schemas.citation import (
    CitationGraphResponse,
    CitationPaperDetailResponse,
    CitationRebuildRequest,
    CitationRebuildResponse,
)
from app.services.citation_service import (
    get_citation_graph,
    get_citation_paper_detail,
    rebuild_citations,
)


router = APIRouter(prefix="/citations", tags=["citations"])


@router.get("/graph", response_model=CitationGraphResponse)
def citation_graph(
    limit: int = Query(default=200, ge=1, le=1000),
    min_citations: int = Query(default=0, ge=0),
    paper_id: int | None = Query(default=None, gt=0),
    include_isolated: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    return success(
        get_citation_graph(
            db,
            limit=limit,
            min_citations=min_citations,
            paper_id=paper_id,
            include_isolated=include_isolated,
        )
    )


@router.get(
    "/papers/{paper_id}",
    response_model=CitationPaperDetailResponse,
)
def citation_paper_detail(
    paper_id: int,
    db: Session = Depends(get_db),
):
    return success(get_citation_paper_detail(db, paper_id))


@router.post("/rebuild", response_model=CitationRebuildResponse)
def rebuild_citation_data(
    request: CitationRebuildRequest | None = Body(default=None),
    db: Session = Depends(get_db),
):
    requested_ids = request.paper_ids if request is not None else None
    statistics = rebuild_citations(db, requested_ids)
    if requested_ids is None:
        rebuilt_ids = [
            paper_id
            for (paper_id,) in db.query(Paper.id)
            .order_by(Paper.id.asc())
            .all()
        ]
    else:
        rebuilt_ids = sorted(set(requested_ids))
    return success(
        {
            **statistics,
            "rebuilt_paper_ids": rebuilt_ids,
        }
    )


@router.post(
    "/papers/{paper_id}/rebuild",
    response_model=CitationRebuildResponse,
)
def rebuild_one_paper_citations(
    paper_id: int,
    db: Session = Depends(get_db),
):
    statistics = rebuild_citations(db, [paper_id])
    return success(
        {
            **statistics,
            "rebuilt_paper_ids": [paper_id],
        }
    )
