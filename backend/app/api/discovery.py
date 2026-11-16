from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.discovery import (
    DiscoveryEnvelope,
    DiscoveryField,
    DiscoveryListEnvelope,
    DiscoverySortBy,
    DiscoverySortOrder,
    FavoriteExternalRequest,
    TrackingSearchCreate,
)
from app.services.discovery_service import (
    create_tracking_search,
    favorite_external_paper,
    get_tracking_search,
    import_favorite_pdf,
    list_external_favorites,
    list_tracking_searches,
    recommend_external_literature,
    refresh_tracking_search,
    search_external_literature,
)


router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.get("/search", response_model=DiscoveryEnvelope)
def search_external(
    query: str = Query(min_length=2, max_length=200),
    field: DiscoveryField = Query(default="all"),
    page: int = Query(default=1, ge=1, le=100),
    page_size: int = Query(default=10, ge=1, le=50),
    sort_by: DiscoverySortBy = Query(default="relevance"),
    sort_order: DiscoverySortOrder = Query(default="descending"),
    db: Session = Depends(get_db),
):
    return success(
        search_external_literature(
            db,
            query=query,
            field=field,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    )


@router.post("/favorites", response_model=DiscoveryEnvelope)
def create_favorite(
    request: FavoriteExternalRequest,
    db: Session = Depends(get_db),
):
    return success(
        favorite_external_paper(db, arxiv_id=request.arxiv_id)
    )


@router.get("/favorites", response_model=DiscoveryListEnvelope)
def get_favorites(db: Session = Depends(get_db)):
    return success(list_external_favorites(db))


@router.post(
    "/favorites/{favorite_id}/import",
    response_model=DiscoveryEnvelope,
)
def import_favorite(
    favorite_id: int,
    db: Session = Depends(get_db),
):
    return success(import_favorite_pdf(db, favorite_id=favorite_id))


@router.post("/tracking", response_model=DiscoveryEnvelope)
def create_tracking(
    request: TrackingSearchCreate,
    db: Session = Depends(get_db),
):
    return success(
        create_tracking_search(
            db,
            name=request.name,
            query=request.query,
            field=request.field,
            sort_by=request.sort_by,
            sort_order=request.sort_order,
            page_size=request.page_size,
        )
    )


@router.get("/tracking", response_model=DiscoveryListEnvelope)
def get_tracking_searches(db: Session = Depends(get_db)):
    return success(list_tracking_searches(db))


@router.get("/tracking/{tracking_id}", response_model=DiscoveryEnvelope)
def get_tracking(
    tracking_id: int,
    db: Session = Depends(get_db),
):
    return success(get_tracking_search(db, tracking_id))


@router.post(
    "/tracking/{tracking_id}/refresh",
    response_model=DiscoveryEnvelope,
)
def refresh_tracking(
    tracking_id: int,
    db: Session = Depends(get_db),
):
    return success(refresh_tracking_search(db, tracking_id))


@router.get("/recommendations", response_model=DiscoveryEnvelope)
def get_recommendations(
    page_size: int = Query(default=8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    return success(
        recommend_external_literature(db, page_size=page_size)
    )
