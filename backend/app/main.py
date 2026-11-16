from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.api.health import router as health_router
from app.api.papers import router as papers_router
from app.api.citations import router as citations_router
from app.api.datasets import router as datasets_router
from app.api.discovery import router as discovery_router
from app.api.reviews import router as reviews_router
from app.api.research_agent import router as research_agent_router
from app.api.writing import router as writing_router
from app.services.embedding_service import delete_paper_vectors
from app.core.exceptions import AppError, app_error_handler, unhandled_error_handler
from app.database import Base, SessionLocal, engine, ensure_additive_schema
from app.models import (  # noqa: F401
    Paper,
    PaperChunk,
    PaperImportItem,
    PaperImportJob,
    PaperIndexRepairLock,
    PaperElement,
    PaperPage,
    PaperVersion,
    ExternalFavorite,
    ExternalPaper,
    TrackingRun,
    TrackingRunItem,
    TrackingSearch,
    TrackingSeen,
    ResearchAgentArtifact,
    ResearchAgentEvidence,
    ResearchAgentNode,
    ResearchAgentRun,
    ResearchAgentToolCall,
)
from app.services.paper_service import ensure_storage, retry_pending_deletions
from app.services.ingestion_service import (
    cleanup_expired_import_files,
    recover_interrupted_jobs,
    run_import_worker,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_storage()
    Base.metadata.create_all(bind=engine)
    ensure_additive_schema(engine)
    Path(get_settings().upload_path).mkdir(parents=True, exist_ok=True)
    retry_pending_deletions(SessionLocal, delete_paper_vectors)
    cleanup_expired_import_files(SessionLocal)
    recover_interrupted_jobs(SessionLocal)
    stop_worker = Event()
    worker = Thread(
        target=run_import_worker,
        args=(SessionLocal, stop_worker),
        name="paper-import-worker",
        daemon=True,
    )
    worker.start()
    try:
        yield
    finally:
        stop_worker.set()
        worker.join(timeout=10)


app = FastAPI(
    title=settings.app_name,
    description=(
        "面向计算机网络领域的科研助手。v0.3.0 开发预览提供可靠论文检索、"
        "文献发现、引用图谱预览、多文献证据综述、基础学术写作、"
        "结构化阅读翻译与 CSV 数据分析；自主科研 Agent 为 v0.4.0 提前预览。"
    ),
    version="0.3.0-dev.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数不合法。",
                "details": exc.errors(),
            },
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": "HTTP_ERROR",
                "message": exc.detail if isinstance(exc.detail, str) else "请求失败。",
                "details": None,
            },
        },
    )


app.include_router(health_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(citations_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")
app.include_router(writing_router, prefix="/api")
app.include_router(datasets_router, prefix="/api")
app.include_router(discovery_router, prefix="/api")
app.include_router(research_agent_router, prefix="/api")
