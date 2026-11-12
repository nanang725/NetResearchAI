from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.api.papers import router as papers_router
from app.config import get_settings
from app.core.exceptions import AppError, app_error_handler, unhandled_error_handler
from app.database import Base, engine
from app.models import Paper, PaperPage  # noqa: F401
from app.services.paper_service import ensure_storage

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_storage()
    Base.metadata.create_all(bind=engine)
    Path(settings.upload_path).mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title=settings.app_name,
    description="面向计算机网络领域的全流程科研助手。第一阶段提供论文库与 PDF 解析。",
    version="0.1.0",
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
