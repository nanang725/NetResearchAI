from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.api.health import router as health_router
from app.api.papers import router as papers_router
from app.services.embedding_service import delete_paper_vectors
from app.core.exceptions import AppError, app_error_handler, unhandled_error_handler
from app.database import Base, SessionLocal, engine
from app.models import Paper, PaperChunk, PaperPage  # noqa: F401
from app.services.paper_service import ensure_storage, retry_pending_deletions

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_storage()
    Base.metadata.create_all(bind=engine)
    Path(get_settings().upload_path).mkdir(parents=True, exist_ok=True)
    retry_pending_deletions(SessionLocal, delete_paper_vectors)
    yield


app = FastAPI(
    title=settings.app_name,
    description=(
        "面向计算机网络领域的科研助手。v0.2.0 提供论文 PDF 管理与混合检索、"
        "最多 12 条请求级对话历史、上下文理解与轻量 Query Rewriting、"
        "五类基础意图识别，以及论文证据约束和不检索论文证据的通用知识回答。"
        "服务端不保存持久化会话状态；科研辅助与学术写作尚无完整专属工作流。"
    ),
    version="0.2.0",
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
