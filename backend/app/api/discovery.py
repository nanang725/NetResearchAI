from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session

from app.core.responses import success
from app.database import get_db
from app.schemas.discovery import (
    DailyInterestsUpdate,
    DailyReadStateUpdate,
    DiscoveryEnvelope,
    DiscoveryField,
    DiscoveryListEnvelope,
    DiscoverySortBy,
    DiscoverySortOrder,
    FavoriteExternalRequest,
    TrackingSearchCreate,
)
from app.services.daily_recommendation_service import (
    execute_daily_refresh,
    get_daily_recommendation_state,
    queue_daily_refresh,
    serialize_daily_batch,
    update_daily_item_read_state,
    update_interests,
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


def _session_factory(db: Session):
    return sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        future=True,
    )


@router.get("/daily", response_model=DiscoveryEnvelope)
def get_daily_recommendations(db: Session = Depends(get_db)):
    return success(get_daily_recommendation_state(db))


@router.put("/interests", response_model=DiscoveryEnvelope)
def save_discovery_interests(
    request: DailyInterestsUpdate,
    db: Session = Depends(get_db),
):
    return success(update_interests(db, request.interests))


@router.post(
    "/daily/refresh",
    status_code=202,
    response_model=DiscoveryEnvelope,
)
def refresh_daily_recommendations(
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    batch = queue_daily_refresh(db, force=force)
    if batch.status == "running":
        background_tasks.add_task(
            execute_daily_refresh,
            batch.id,
            _session_factory(db),
        )
    return success(serialize_daily_batch(db, batch), status_code=202)


@router.patch(
    "/daily/items/{item_id}/read",
    response_model=DiscoveryEnvelope,
)
def set_daily_recommendation_read_state(
    item_id: int,
    request: DailyReadStateUpdate,
    db: Session = Depends(get_db),
):
    return success(
        update_daily_item_read_state(
            db,
            item_id,
            is_read=request.is_read,
        )
    )


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
