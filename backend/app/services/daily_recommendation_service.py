from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.discovery import (
    DailyRecommendationBatch,
    DailyRecommendationItem,
    DiscoveryInterestProfile,
    ExternalFavorite,
    ExternalPaper,
)
from app.services.arxiv_service import search_arxiv
from app.services.discovery_service import (
    _find_local_paper_for_arxiv,
    _serialize_favorite,
    _serialize_external,
    _upsert_external_paper,
)


DEFAULT_INTERESTS = [
    "大语言模型",
    "RAG",
    "AI Agent",
    "计算机网络",
]

INTEREST_TERMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "人工智能": (
        "artificial intelligence",
        ("artificial intelligence", "machine intelligence"),
    ),
    "大语言模型": (
        "large language model",
        (
            "large language model",
            "large language models",
            "language model",
            "llm",
            "llms",
        ),
    ),
    "RAG": (
        "retrieval augmented generation",
        (
            "retrieval augmented generation",
            "retrieval-augmented generation",
            "retrieval augmented",
            "retrieval-augmented",
            "rag",
        ),
    ),
    "AI Agent": (
        "AI agent",
        (
            "ai agent",
            "ai agents",
            "agentic",
            "autonomous agent",
            "multi-agent",
            "multi agent",
        ),
    ),
    "计算机网络": (
        "computer network",
        (
            "computer network",
            "computer networks",
            "networking",
            "network traffic",
            "network protocol",
            "network protocols",
        ),
    ),
    "知识图谱": (
        "knowledge graph",
        ("knowledge graph", "knowledge graphs"),
    ),
    "文献智能分析": (
        "scientific literature analysis",
        (
            "scientific literature",
            "scholarly literature",
            "literature analysis",
            "literature mining",
            "research literature",
        ),
    ),
}

DAILY_RESULT_LIMIT = 10
ARXIV_PAGE_SIZE = 25
HISTORY_DEDUPE_DAYS = 7


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _today_key() -> str:
    return datetime.now().astimezone().date().isoformat()


def normalize_interests(interests: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in interests:
        value = str(raw).strip()
        if not value:
            continue
        if len(value) < 2 or len(value) > 80:
            raise AppError(
                "研究兴趣长度必须为 2 至 80 个字符。",
                code="INVALID_DISCOVERY_INTERESTS",
                status_code=422,
                details={"interest": value[:80]},
            )
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    if not normalized:
        raise AppError(
            "请至少配置一个研究兴趣。",
            code="INVALID_DISCOVERY_INTERESTS",
            status_code=422,
        )
    if len(normalized) > 8:
        raise AppError(
            "研究兴趣最多配置 8 个。",
            code="INVALID_DISCOVERY_INTERESTS",
            status_code=422,
        )
    return normalized


def _profile_interests(profile: DiscoveryInterestProfile | None) -> list[str]:
    if profile is None:
        return list(DEFAULT_INTERESTS)
    persisted = _loads(profile.interests_json, [])
    if not isinstance(persisted, list):
        return list(DEFAULT_INTERESTS)
    try:
        return normalize_interests(str(item) for item in persisted)
    except AppError:
        return list(DEFAULT_INTERESTS)


def _serialize_profile(profile: DiscoveryInterestProfile | None) -> dict[str, Any]:
    return {
        "interests": _profile_interests(profile),
        "configured": profile is not None,
        "updated_at": (
            profile.updated_at.isoformat()
            if profile is not None and profile.updated_at
            else None
        ),
    }


def get_interest_profile(db: Session) -> DiscoveryInterestProfile | None:
    return db.get(DiscoveryInterestProfile, 1)


def update_interests(
    db: Session,
    interests: Iterable[str],
) -> dict[str, Any]:
    normalized = normalize_interests(interests)
    profile = get_interest_profile(db)
    if profile is None:
        profile = DiscoveryInterestProfile(id=1, interests_json="[]")
        db.add(profile)
    profile.interests_json = _json_dump(normalized)
    db.commit()
    db.refresh(profile)
    return _serialize_profile(profile)


def _interest_search_terms(interest: str) -> tuple[str, tuple[str, ...]]:
    return INTEREST_TERMS.get(
        interest,
        (interest, (interest,)),
    )


def _matches_term(text: str, term: str) -> bool:
    escaped = re.escape(term.casefold())
    return re.search(rf"(?<!\w){escaped}(?!\w)", text) is not None


def _freshness_score(published: str | None) -> float:
    if not published:
        return 0.0
    try:
        published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_days = max(
        0.0,
        (datetime.now(timezone.utc) - published_at.astimezone(timezone.utc))
        .total_seconds()
        / 86400,
    )
    return max(0.0, 1.0 - min(age_days, 30.0) / 30.0)


def rank_daily_candidate(
    result: dict[str, Any],
    interests: list[str],
) -> dict[str, Any] | None:
    title = str(result.get("title") or "").casefold()
    summary = str(result.get("summary") or "").casefold()
    categories = " ".join(
        str(item) for item in result.get("categories", [])
    ).casefold()
    matched: list[str] = []
    title_matches = 0
    abstract_matches = 0
    for interest in interests:
        _phrase, terms = _interest_search_terms(interest)
        hit_title = any(_matches_term(title, term) for term in terms)
        hit_summary = any(_matches_term(summary, term) for term in terms)
        hit_category = any(_matches_term(categories, term) for term in terms)
        if hit_title or hit_summary or hit_category:
            matched.append(interest)
            title_matches += int(hit_title)
            abstract_matches += int(hit_summary or hit_category)
    if not matched:
        return None
    coverage = len(matched) / max(1, len(interests))
    score = (
        coverage * 3.0
        + min(title_matches, 3) * 0.8
        + min(abstract_matches, 3) * 0.3
        + _freshness_score(result.get("published")) * 1.5
    )
    published = result.get("published")
    freshness = (
        f"提交于 {published[:10]}"
        if isinstance(published, str) and len(published) >= 10
        else "提交日期未知"
    )
    reason = (
        f"匹配研究兴趣：{'、'.join(matched)}；{freshness}。"
        "排序依据为关键词覆盖、标题命中与提交时间，"
        "推荐理由由确定性规则生成，不代表论文结论。"
    )
    return {
        **result,
        "score": round(score, 3),
        "matched_interests": matched,
        "recommendation_reason": reason,
    }


def rank_daily_candidates(
    results: Iterable[dict[str, Any]],
    interests: list[str],
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for result in results:
        arxiv_id = str(result.get("arxiv_id") or "")
        if not arxiv_id or arxiv_id in unique:
            continue
        ranked = rank_daily_candidate(result, interests)
        if ranked is not None:
            unique[arxiv_id] = ranked
    return sorted(
        unique.values(),
        key=lambda item: (
            float(item["score"]),
            str(item.get("published") or ""),
        ),
        reverse=True,
    )


def _serialize_daily_item(
    db: Session,
    item: DailyRecommendationItem,
) -> dict[str, Any]:
    external = item.external_paper
    favorite = (
        db.query(ExternalFavorite)
        .filter(ExternalFavorite.external_paper_id == external.id)
        .first()
    )
    local_paper = _find_local_paper_for_arxiv(
        db,
        external.arxiv_id,
        favorite,
    )
    favorite_state = (
        _serialize_favorite(db, favorite)
        if favorite is not None
        else None
    )
    return {
        "item_id": item.id,
        **_serialize_external(
            external,
            local_paper=local_paper,
            favorite=favorite,
        ),
        "score": item.score,
        "matched_interests": _loads(item.matched_interests_json, []),
        "recommendation_reason": item.recommendation_reason,
        "reason_source": "deterministic_keyword_rules",
        "is_read": item.is_read,
        "position": item.position,
        "favorite_import_status": (
            favorite_state["import_status"]
            if favorite_state is not None
            else "not_imported"
        ),
        "favorite_import_error": (
            favorite_state["import_error"]
            if favorite_state is not None
            else None
        ),
    }


def serialize_daily_batch(
    db: Session,
    batch: DailyRecommendationBatch | None,
) -> dict[str, Any] | None:
    if batch is None:
        return None
    items = sorted(batch.items, key=lambda item: item.position)
    return {
        "batch_id": batch.id,
        "date": batch.recommendation_date,
        "status": batch.status,
        "interests": _loads(batch.interests_json, []),
        "new_item_count": batch.new_item_count,
        "error": batch.error_message,
        "started_at": (
            batch.started_at.isoformat() if batch.started_at else None
        ),
        "finished_at": (
            batch.finished_at.isoformat() if batch.finished_at else None
        ),
        "created_at": batch.created_at.isoformat(),
        "items": [_serialize_daily_item(db, item) for item in items],
        "source": "arxiv_official_api",
        "reason_disclaimer": (
            "推荐理由是根据研究兴趣、标题/摘要关键词与提交时间生成的排序说明，"
            "不是论文事实，也不是 AI 对论文内容的新结论。"
        ),
    }


def get_daily_recommendation_state(db: Session) -> dict[str, Any]:
    today = _today_key()
    current = (
        db.query(DailyRecommendationBatch)
        .filter(DailyRecommendationBatch.recommendation_date == today)
        .first()
    )
    latest = (
        db.query(DailyRecommendationBatch)
        .order_by(DailyRecommendationBatch.recommendation_date.desc())
        .first()
    )
    return {
        "date": today,
        "profile": _serialize_profile(get_interest_profile(db)),
        "current": serialize_daily_batch(db, current),
        "latest": serialize_daily_batch(db, latest),
    }


def queue_daily_refresh(
    db: Session,
    *,
    force: bool,
) -> DailyRecommendationBatch:
    today = _today_key()
    batch = (
        db.query(DailyRecommendationBatch)
        .filter(DailyRecommendationBatch.recommendation_date == today)
        .first()
    )
    if batch is not None and batch.status == "running":
        return batch
    if batch is not None and batch.status == "completed" and not force:
        return batch
    interests = _profile_interests(get_interest_profile(db))
    if batch is None:
        batch = DailyRecommendationBatch(
            recommendation_date=today,
            interests_json=_json_dump(interests),
            status="running",
        )
        db.add(batch)
    else:
        batch.interests_json = _json_dump(interests)
        batch.status = "running"
    batch.error_message = None
    batch.new_item_count = 0
    batch.started_at = datetime.utcnow()
    batch.finished_at = None
    db.commit()
    db.refresh(batch)
    return batch


def execute_daily_refresh(
    batch_id: int,
    session_factory: Callable[[], Session],
) -> None:
    db = session_factory()
    batch: DailyRecommendationBatch | None = None
    try:
        batch = db.get(DailyRecommendationBatch, batch_id)
        if batch is None or batch.status != "running":
            return
        interests = _loads(batch.interests_json, [])
        raw_results: list[dict[str, Any]] = []
        failures: list[str] = []
        failure_messages: list[str] = []
        successful_interests = 0
        for interest in interests:
            phrase, _terms = _interest_search_terms(interest)
            payload: dict[str, Any] | None = None
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    payload = search_arxiv(
                        query=phrase,
                        field="all",
                        page=1,
                        page_size=ARXIV_PAGE_SIZE,
                        sort_by="submitted",
                        sort_order="descending",
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if payload is None:
                message = getattr(last_error, "message", None) or (
                    str(last_error) if last_error else "未知错误"
                )
                failures.append(f"{interest}：{message}")
                failure_messages.append(message)
                continue
            successful_interests += 1
            page_results = payload.get("results", [])
            if not isinstance(page_results, list):
                page_results = []
            raw_results.extend(page_results)
        if successful_interests == 0 and failures:
            unique_messages = list(dict.fromkeys(failure_messages))
            error_message = (
                unique_messages[0]
                if len(unique_messages) == 1
                else "arXiv 暂无可用响应：" + "；".join(failures)
            )
            raise AppError(
                error_message,
                code="ARXIV_DAILY_UNAVAILABLE",
                status_code=502,
                details={"failures": failures},
            )

        cutoff = (
            datetime.now().astimezone().date()
            - timedelta(days=HISTORY_DEDUPE_DAYS)
        ).isoformat()
        recent_ids = {
            external_id
            for (external_id,) in (
                db.query(DailyRecommendationItem.external_paper_id)
                .join(DailyRecommendationBatch)
                .filter(DailyRecommendationBatch.recommendation_date >= cutoff)
                .all()
            )
        }
        ranked = rank_daily_candidates(raw_results, interests)
        added = 0
        start_position = max(
            (item.position for item in batch.items),
            default=-1,
        ) + 1
        for candidate in ranked:
            if added >= DAILY_RESULT_LIMIT:
                break
            external = _upsert_external_paper(db, candidate)
            if external.id in recent_ids:
                continue
            db.add(
                DailyRecommendationItem(
                    batch_id=batch.id,
                    external_paper_id=external.id,
                    position=start_position + added,
                    score=float(candidate["score"]),
                    recommendation_reason=candidate["recommendation_reason"],
                    matched_interests_json=_json_dump(
                        candidate["matched_interests"]
                    ),
                )
            )
            recent_ids.add(external.id)
            added += 1
        batch = db.get(DailyRecommendationBatch, batch_id)
        if batch is None:
            return
        batch.status = "completed"
        batch.new_item_count = added
        batch.error_message = (
            "部分研究兴趣更新失败：" + "；".join(failures)
            if failures
            else None
        )
        batch.finished_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        batch = db.get(DailyRecommendationBatch, batch_id)
        if batch is not None:
            batch.status = "failed"
            batch.error_message = getattr(exc, "message", str(exc))[:2000]
            batch.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def update_daily_item_read_state(
    db: Session,
    item_id: int,
    *,
    is_read: bool,
) -> dict[str, Any]:
    item = db.get(DailyRecommendationItem, item_id)
    if item is None:
        raise AppError(
            "每日推荐条目不存在。",
            code="DAILY_RECOMMENDATION_NOT_FOUND",
            status_code=404,
            details={"item_id": item_id},
        )
    item.is_read = is_read
    db.commit()
    db.refresh(item)
    return _serialize_daily_item(db, item)
