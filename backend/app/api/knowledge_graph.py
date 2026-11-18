from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.knowledge_graph import (
    KnowledgeGraphEnvelope,
    KnowledgeGraphRebuildRequest,
)
from app.services.knowledge_graph_service import (
    compare_methods,
    explore_research_gaps,
    find_relation_path,
    get_core_papers,
    get_knowledge_graph,
    get_knowledge_node_detail,
    rebuild_and_summarize,
)


router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])


@router.get("", response_model=KnowledgeGraphEnvelope)
def knowledge_graph(
    node_types: list[str] = Query(default=[]),
    relation_types: list[str] = Query(default=[]),
    paper_id: int | None = Query(default=None, gt=0),
    query: str = Query(default="", max_length=300),
    limit: int = Query(default=300, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    return success(
        get_knowledge_graph(
            db,
            node_types=node_types or None,
            relation_types=relation_types or None,
            paper_id=paper_id,
            query=query,
            limit=limit,
        )
    )


@router.get(
    "/entities/{node_id}",
    response_model=KnowledgeGraphEnvelope,
)
def knowledge_graph_node(
    node_id: int,
    db: Session = Depends(get_db),
):
    return success(get_knowledge_node_detail(db, node_id))


@router.get(
    "/analysis/core-papers",
    response_model=KnowledgeGraphEnvelope,
)
def core_papers(
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return success(get_core_papers(db, limit=limit))


@router.get(
    "/analysis/method-comparison",
    response_model=KnowledgeGraphEnvelope,
)
def method_comparison(
    paper_ids: list[int] = Query(min_length=2, max_length=5),
    db: Session = Depends(get_db),
):
    return success(compare_methods(db, paper_ids=paper_ids))


@router.get("/path", response_model=KnowledgeGraphEnvelope)
def relation_path(
    source_paper_id: int = Query(gt=0),
    target_paper_id: int = Query(gt=0),
    max_depth: int = Query(default=5, ge=1, le=8),
    db: Session = Depends(get_db),
):
    return success(
        find_relation_path(
            db,
            source_paper_id=source_paper_id,
            target_paper_id=target_paper_id,
            max_depth=max_depth,
        )
    )


@router.get(
    "/research-gaps",
    response_model=KnowledgeGraphEnvelope,
)
def research_gaps(
    topic: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return success(
        explore_research_gaps(
            db,
            topic=topic,
            limit=limit,
        )
    )


@router.post("/rebuild", response_model=KnowledgeGraphEnvelope)
def rebuild_graph(
    request: KnowledgeGraphRebuildRequest | None = Body(default=None),
    db: Session = Depends(get_db),
):
    paper_ids = request.paper_ids if request is not None else None
    return success(rebuild_and_summarize(db, paper_ids))
