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
            "version": "0.4.0",
            "features": {
                "pdf_upload": "ready",
                "pdf_parse": "ready",
                "paper_library": "ready",
                "rag": "ready",
                "agent": "ready",
                "paper_qa": "ready",
                "discovery": "ready",
                "citation_graph": "preview",
                "multi_paper_review": "ready",
                "academic_writing": "basic",
                "structured_reading_translation": "ready",
                "data_analysis": "ready",
                "research_agent": "ready_read_only",
                "langgraph_agent": "planned",
                "scientific_knowledge_graph": "planned_v0.5.0",
                "version_compare": "not_implemented",
            },
        }
    )
