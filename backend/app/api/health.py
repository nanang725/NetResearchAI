from fastapi import APIRouter

from app.config import get_settings
from app.core.responses import success

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check():
    settings = get_settings()
    return success(
        {
            "name": settings.app_name,
            "status": "ok",
            "env": settings.app_env,
            "version": "0.1.0",
            "features": {
                "pdf_upload": "ready",
                "pdf_parse": "ready",
                "paper_library": "ready",
                "rag": "planned",
                "agent": "planned",
                "citation_graph": "planned",
                "version_compare": "planned",
            },
        }
    )
