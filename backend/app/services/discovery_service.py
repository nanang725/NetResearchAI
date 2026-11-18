from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.discovery import (
    ExternalFavorite,
    ExternalPaper,
    TrackingRun,
    TrackingRunItem,
    TrackingSearch,
    TrackingSeen,
)
from app.models.paper import Paper, PaperImportItem, PaperImportJob
from app.services.arxiv_service import (
    ARXIV_TIMEOUT_SECONDS,
    get_arxiv_paper,
    search_arxiv,
)
from app.services.ingestion_service import (
    IMPORT_DIRECTORY,
    create_import_job,
    serialize_import_job,
)
from app.services.retrieval_service import search_chunks_hybrid


logger = logging.getLogger(__name__)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _external_authors(external: ExternalPaper) -> list[str]:
    try:
        value = json.loads(external.authors_json)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _find_local_paper_for_arxiv(
    db: Session,
    arxiv_id: str,
    favorite: ExternalFavorite | None = None,
) -> Paper | None:
    if favorite is not None and favorite.local_paper_id is not None:
        paper = db.query(Paper).filter(Paper.id == favorite.local_paper_id).first()
        if paper is not None:
            return paper
    prefix = f"{arxiv_id.casefold()}%"
    return (
        db.query(Paper)
        .filter(func.lower(Paper.original_filename).like(prefix))
        .order_by(Paper.id.asc())
        .first()
    )


def _serialize_external(
    external: ExternalPaper,
    *,
    local_paper: Paper | None = None,
    favorite: ExternalFavorite | None = None,
) -> dict[str, Any]:
    return {
        "external_paper_id": external.id,
        "arxiv_id": external.arxiv_id,
        "title": external.title,
        "authors": _external_authors(external),
        "summary": external.summary,
        "published": (
            external.published_at.replace(tzinfo=timezone.utc).isoformat()
            if external.published_at
            else None
        ),
        "updated": (
            external.source_updated_at.replace(tzinfo=timezone.utc).isoformat()
            if external.source_updated_at
            else None
        ),
        "primary_category": external.primary_category,
        "abs_url": external.abs_url,
        "pdf_url": external.pdf_url,
        "origin": "arxiv_external",
        "in_local_library": local_paper is not None,
        "local_paper_id": local_paper.id if local_paper else None,
        "favorite_id": favorite.id if favorite else None,
    }


def annotate_external_results(
    db: Session,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    annotated = []
    for result in results:
        favorite = (
            db.query(ExternalFavorite)
            .join(ExternalPaper)
            .filter(ExternalPaper.arxiv_id == result["arxiv_id"])
            .first()
        )
        local_paper = _find_local_paper_for_arxiv(
            db,
            result["arxiv_id"],
            favorite,
        )
        annotated.append(
            {
                **result,
                "origin": "arxiv_external",
                "in_local_library": local_paper is not None,
                "local_paper_id": local_paper.id if local_paper else None,
                "favorite_id": favorite.id if favorite else None,
            }
        )
    return annotated


def search_external_literature(
    db: Session,
    *,
    query: str,
    field: str,
    page: int,
    page_size: int,
    sort_by: str,
    sort_order: str,
) -> dict[str, Any]:
    payload = search_arxiv(
        query=query,
        field=field,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    payload["results"] = annotate_external_results(db, payload["results"])
    return payload


def _upsert_external_paper(
    db: Session,
    result: dict[str, Any],
) -> ExternalPaper:
    external = (
        db.query(ExternalPaper)
        .filter(ExternalPaper.arxiv_id == result["arxiv_id"])
        .first()
    )
    if external is None:
        external = ExternalPaper(
            arxiv_id=result["arxiv_id"],
            title=result["title"],
            authors_json=json.dumps(
                result.get("authors", []),
                ensure_ascii=False,
            ),
            summary=result.get("summary", ""),
            published_at=_parse_iso_datetime(result.get("published")),
            source_updated_at=_parse_iso_datetime(result.get("updated")),
            primary_category=result.get("primary_category"),
            abs_url=result["abs_url"],
            pdf_url=result["pdf_url"],
        )
        db.add(external)
    else:
        external.title = result["title"]
        external.authors_json = json.dumps(
            result.get("authors", []),
            ensure_ascii=False,
        )
        external.summary = result.get("summary", "")
        external.published_at = _parse_iso_datetime(result.get("published"))
        external.source_updated_at = _parse_iso_datetime(
            result.get("updated")
        )
        external.primary_category = result.get("primary_category")
        external.abs_url = result["abs_url"]
        external.pdf_url = result["pdf_url"]
    db.flush()
    return external


def _serialize_favorite(
    db: Session,
    favorite: ExternalFavorite,
) -> dict[str, Any]:
    import_job = (
        db.query(PaperImportJob)
        .filter(PaperImportJob.id == favorite.import_job_id)
        .first()
        if favorite.import_job_id
        else None
    )
    if import_job is not None:
        item = next(
            (
                candidate
                for candidate in import_job.items
                if candidate.id == favorite.import_item_id
            ),
            None,
        )
        if item is not None:
            favorite.import_status = (
                "imported"
                if item.status == "succeeded" and item.paper_id is not None
                else item.status
            )
            favorite.import_error = item.error_message
            if item.paper_id is not None:
                favorite.local_paper_id = item.paper_id
    local_paper = (
        db.query(Paper).filter(Paper.id == favorite.local_paper_id).first()
        if favorite.local_paper_id
        else _find_local_paper_for_arxiv(
            db,
            favorite.external_paper.arxiv_id,
            favorite,
        )
    )
    return {
        **_serialize_external(
            favorite.external_paper,
            local_paper=local_paper,
            favorite=favorite,
        ),
        "favorite_created_at": favorite.created_at.isoformat(),
        "import_status": favorite.import_status,
        "import_error": favorite.import_error,
        "import_job": serialize_import_job(import_job) if import_job else None,
    }


def favorite_external_paper(
    db: Session,
    *,
    arxiv_id: str,
) -> dict[str, Any]:
    verified = get_arxiv_paper(arxiv_id)
    external = _upsert_external_paper(db, verified)
    favorite = (
        db.query(ExternalFavorite)
        .filter(ExternalFavorite.external_paper_id == external.id)
        .first()
    )
    if favorite is None:
        favorite = ExternalFavorite(external_paper_id=external.id)
        db.add(favorite)
        db.flush()
    local_paper = _find_local_paper_for_arxiv(db, external.arxiv_id, favorite)
    if local_paper is not None:
        favorite.local_paper_id = local_paper.id
        favorite.import_status = "already_in_library"
    db.commit()
    db.refresh(favorite)
    return _serialize_favorite(db, favorite)


def list_external_favorites(db: Session) -> list[dict[str, Any]]:
    favorites = (
        db.query(ExternalFavorite)
        .order_by(ExternalFavorite.created_at.desc())
        .all()
    )
    payload = [_serialize_favorite(db, favorite) for favorite in favorites]
    if any(favorite.import_job_id for favorite in favorites):
        db.commit()
    return payload


def download_arxiv_pdf(url: str, max_bytes: int) -> bytes:
    try:
        with httpx.Client(
            timeout=ARXIV_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "NetResearchAI/0.5.0 "
                    "(local academic literature import MVP)"
                ),
                "Accept": "application/pdf",
            },
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 429:
                    raise AppError(
                        "arXiv PDF 下载受到限流，请稍后重试。",
                        code="ARXIV_PDF_RATE_LIMITED",
                        status_code=429,
                        details={
                            "retry_after": response.headers.get("Retry-After")
                        },
                    )
                if response.status_code >= 400:
                    raise AppError(
                        "arXiv PDF 下载失败。",
                        code="ARXIV_PDF_HTTP_ERROR",
                        status_code=502,
                        details={"upstream_status": response.status_code},
                    )
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise AppError(
                            "arXiv PDF 超过论文导入大小限制。",
                            code="ARXIV_PDF_TOO_LARGE",
                            status_code=413,
                            details={"max_bytes": max_bytes},
                        )
                    chunks.append(chunk)
    except AppError:
        raise
    except httpx.TimeoutException as exc:
        raise AppError(
            "arXiv PDF 下载超时。",
            code="ARXIV_PDF_TIMEOUT",
            status_code=504,
        ) from exc
    except httpx.RequestError as exc:
        raise AppError(
            "无法连接 arXiv PDF 下载服务。",
            code="ARXIV_PDF_NETWORK_ERROR",
            status_code=502,
            details={"error_type": type(exc).__name__},
        ) from exc
    content = b"".join(chunks)
    if not content.startswith(b"%PDF"):
        raise AppError(
            "arXiv 返回的内容不是有效 PDF。",
            code="ARXIV_PDF_INVALID",
            status_code=502,
        )
    return content


def import_favorite_pdf(
    db: Session,
    *,
    favorite_id: int,
) -> dict[str, Any]:
    favorite = (
        db.query(ExternalFavorite)
        .filter(ExternalFavorite.id == favorite_id)
        .first()
    )
    if favorite is None:
        raise AppError(
            "收藏记录不存在。",
            code="EXTERNAL_FAVORITE_NOT_FOUND",
            status_code=404,
        )
    external = favorite.external_paper
    local_paper = _find_local_paper_for_arxiv(
        db,
        external.arxiv_id,
        favorite,
    )
    if local_paper is not None:
        favorite.local_paper_id = local_paper.id
        favorite.import_status = "already_in_library"
        favorite.import_error = None
        db.commit()
        return {
            "status": "already_in_library",
            "favorite": _serialize_favorite(db, favorite),
            "import_job": None,
        }

    settings = get_settings()
    try:
        content = download_arxiv_pdf(
            external.pdf_url,
            settings.max_upload_bytes,
        )
    except AppError as exc:
        favorite.import_status = "failed"
        favorite.import_error = exc.message
        db.commit()
        raise

    file_hash = hashlib.sha256(content).hexdigest()
    duplicate = (
        db.query(Paper)
        .filter(Paper.file_hash == file_hash)
        .first()
    )
    if duplicate is not None:
        favorite.local_paper_id = duplicate.id
        favorite.import_status = "already_in_library"
        favorite.import_error = None
        db.commit()
        return {
            "status": "already_in_library",
            "favorite": _serialize_favorite(db, favorite),
            "import_job": None,
        }

    staging_root = (settings.upload_path / IMPORT_DIRECTORY).resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    filename = f"{external.arxiv_id.replace('/', '_')}.pdf"
    staged_path = staging_root / f"{uuid4()}.pdf"
    try:
        with staged_path.open("xb") as output:
            output.write(content)
        job = create_import_job(
            db,
            [
                {
                    "path": staged_path,
                    "filename": filename,
                    "size": len(content),
                }
            ],
        )
        item = job.items[0]
        favorite.import_status = "queued"
        favorite.import_error = None
        favorite.import_job_id = job.id
        favorite.import_item_id = item.id
        db.commit()
    except Exception:
        staged_path.unlink(missing_ok=True)
        db.rollback()
        raise
    return {
        "status": "queued",
        "favorite": _serialize_favorite(db, favorite),
        "import_job": serialize_import_job(job),
    }


def create_tracking_search(
    db: Session,
    *,
    name: str,
    query: str,
    field: str,
    sort_by: str,
    sort_order: str,
    page_size: int,
) -> dict[str, Any]:
    tracking = TrackingSearch(
        name=name.strip(),
        query=query.strip(),
        field=field,
        sort_by=sort_by,
        sort_order=sort_order,
        page_size=page_size,
    )
    db.add(tracking)
    db.commit()
    db.refresh(tracking)
    return _serialize_tracking(db, tracking)


def _serialize_run(run: TrackingRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "tracking_id": run.tracking_id,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "total_results": run.total_results,
        "new_result_count": run.new_result_count,
        "error_message": run.error_message,
        "results": [
            {
                **_serialize_external(item.external_paper),
                "is_new": item.is_new,
                "run_position": item.position,
            }
            for item in run.items
        ],
    }


def _serialize_tracking(
    db: Session,
    tracking: TrackingSearch,
    *,
    include_latest_results: bool = True,
) -> dict[str, Any]:
    runs = (
        db.query(TrackingRun)
        .filter(TrackingRun.tracking_id == tracking.id)
        .order_by(TrackingRun.id.desc())
        .limit(20)
        .all()
    )
    latest = runs[0] if runs else None
    return {
        "tracking_id": tracking.id,
        "name": tracking.name,
        "query": tracking.query,
        "field": tracking.field,
        "sort_by": tracking.sort_by,
        "sort_order": tracking.sort_order,
        "page_size": tracking.page_size,
        "created_at": tracking.created_at.isoformat(),
        "last_refreshed_at": (
            tracking.last_refreshed_at.isoformat()
            if tracking.last_refreshed_at
            else None
        ),
        "run_count": len(runs),
        "new_count_latest": latest.new_result_count if latest else 0,
        "latest_run": _serialize_run(latest) if latest else None,
        "history": [
            {
                key: value
                for key, value in _serialize_run(run).items()
                if key != "results" or include_latest_results and run is latest
            }
            for run in runs
        ],
    }


def list_tracking_searches(db: Session) -> list[dict[str, Any]]:
    trackings = (
        db.query(TrackingSearch)
        .order_by(TrackingSearch.created_at.desc())
        .all()
    )
    return [
        _serialize_tracking(db, tracking, include_latest_results=False)
        for tracking in trackings
    ]


def get_tracking_search(db: Session, tracking_id: int) -> dict[str, Any]:
    tracking = (
        db.query(TrackingSearch)
        .filter(TrackingSearch.id == tracking_id)
        .first()
    )
    if tracking is None:
        raise AppError(
            "追踪任务不存在。",
            code="TRACKING_NOT_FOUND",
            status_code=404,
        )
    return _serialize_tracking(db, tracking)


def refresh_tracking_search(
    db: Session,
    tracking_id: int,
) -> dict[str, Any]:
    tracking = (
        db.query(TrackingSearch)
        .filter(TrackingSearch.id == tracking_id)
        .first()
    )
    if tracking is None:
        raise AppError(
            "追踪任务不存在。",
            code="TRACKING_NOT_FOUND",
            status_code=404,
        )
    run = TrackingRun(tracking_id=tracking.id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        payload = search_arxiv(
            query=tracking.query,
            field=tracking.field,
            page=1,
            page_size=tracking.page_size,
            sort_by=tracking.sort_by,
            sort_order=tracking.sort_order,
        )
        seen_ids = {
            row.external_paper_id
            for row in db.query(TrackingSeen)
            .filter(TrackingSeen.tracking_id == tracking.id)
            .all()
        }
        new_ids: set[int] = set()
        for position, result in enumerate(payload["results"]):
            external = _upsert_external_paper(db, result)
            if external.id not in seen_ids:
                db.add(
                    TrackingSeen(
                        tracking_id=tracking.id,
                        external_paper_id=external.id,
                    )
                )
                seen_ids.add(external.id)
                new_ids.add(external.id)
            db.add(
                TrackingRunItem(
                    run_id=run.id,
                    external_paper_id=external.id,
                    is_new=external.id in new_ids,
                    position=position,
                )
            )
        now = datetime.utcnow()
        run.status = "completed"
        run.finished_at = now
        run.total_results = len(payload["results"])
        run.new_result_count = len(new_ids)
        tracking.last_refreshed_at = now
        db.commit()
    except Exception as exc:
        db.rollback()
        failed_run = (
            db.query(TrackingRun)
            .filter(TrackingRun.id == run.id)
            .first()
        )
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.finished_at = datetime.utcnow()
            failed_run.error_message = getattr(exc, "message", str(exc))[:2000]
            tracked = (
                db.query(TrackingSearch)
                .filter(TrackingSearch.id == tracking.id)
                .first()
            )
            if tracked is not None:
                tracked.last_refreshed_at = datetime.utcnow()
            db.commit()
        raise
    return get_tracking_search(db, tracking.id)


def recommend_external_literature(
    db: Session,
    *,
    page_size: int,
) -> dict[str, Any]:
    latest_tracking = (
        db.query(TrackingSearch)
        .order_by(TrackingSearch.updated_at.desc())
        .first()
    )
    latest_favorite = (
        db.query(ExternalFavorite)
        .order_by(ExternalFavorite.created_at.desc())
        .first()
    )
    if latest_tracking is None and latest_favorite is None:
        raise AppError(
            "请先收藏外部论文或创建追踪关键词，再生成推荐。",
            code="DISCOVERY_NO_RECOMMENDATION_SEED",
            status_code=422,
        )
    if latest_tracking is not None:
        seed_query = latest_tracking.query
        seed_source = "tracking_keyword"
    else:
        seed_query = latest_favorite.external_paper.title
        seed_source = "favorite_paper"

    payload = search_arxiv(
        query=seed_query,
        field="all",
        page=1,
        page_size=page_size,
        sort_by="relevance",
        sort_order="descending",
    )
    payload["results"] = annotate_external_results(db, payload["results"])

    local_recommendations = []
    local_status = "available"
    try:
        local_results = search_chunks_hybrid(
            db=db,
            query=seed_query,
            limit=8,
        )
    except Exception as exc:
        logger.warning("Local RAG recommendation unavailable: %s", type(exc).__name__)
        local_results = []
        local_status = "unavailable"
    seen_local: set[int] = set()
    for result in local_results:
        paper_id = int(result.get("paper_id", 0))
        if not paper_id or paper_id in seen_local:
            continue
        paper = db.query(Paper).filter(Paper.id == paper_id).first()
        if paper is None:
            continue
        seen_local.add(paper_id)
        local_recommendations.append(
            {
                "origin": "local_library",
                "paper_id": paper.id,
                "title": paper.title,
                "authors": paper.authors,
                "publication_year": paper.publication_year,
                "page_number": int(result.get("page_number", 1)),
                "chunk_id": int(result.get("chunk_id", 0)),
                "evidence": str(result.get("text", ""))[:700],
            }
        )
    return {
        "seed": {
            "source": seed_source,
            "query": seed_query,
        },
        "external_results": payload["results"],
        "local_recommendations": local_recommendations,
        "local_rag_status": local_status,
        "source_distinction": {
            "arxiv_external": "来自 arXiv 官方公开 API 的外部搜索结果",
            "local_library": "来自当前本地论文库与现有 RAG 检索",
        },
        "disclaimer": (
            "推荐不会补造论文；外部结果必须经 arXiv 官方接口返回，"
            "本地结果必须对应现有本地论文与 Chunk。"
        ),
    }
