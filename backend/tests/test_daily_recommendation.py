from __future__ import annotations

from datetime import datetime, timezone

from app.core.exceptions import AppError
from app.models.discovery import (
    DailyRecommendationBatch,
    DailyRecommendationItem,
)
from app.services import daily_recommendation_service


def _daily_result(
    arxiv_id: str,
    title: str,
    summary: str,
) -> dict:
    published = datetime.now(timezone.utc).isoformat()
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": ["Daily Author"],
        "summary": summary,
        "published": published,
        "updated": published,
        "primary_category": "cs.AI",
        "categories": ["cs.AI", "cs.CL"],
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "origin": "arxiv_external",
    }


def test_daily_ranking_deduplicates_and_prioritizes_matching_new_papers():
    interests = ["RAG", "大语言模型", "计算机网络"]
    results = [
        _daily_result(
            "2601.00001",
            "Retrieval Augmented Generation for Reliable Answers",
            "A large language model with retrieval-augmented generation.",
        ),
        _daily_result(
            "2601.00001",
            "Duplicate Result",
            "Duplicate metadata must not be recommended twice.",
        ),
        _daily_result(
            "2601.00002",
            "Computer Network Telemetry",
            "A network traffic measurement study.",
        ),
        _daily_result(
            "2601.00003",
            "Unrelated Algebraic Geometry",
            "A pure mathematics manuscript.",
        ),
    ]

    ranked = daily_recommendation_service.rank_daily_candidates(
        results,
        interests,
    )

    assert [item["arxiv_id"] for item in ranked] == [
        "2601.00001",
        "2601.00002",
    ]
    assert ranked[0]["score"] >= ranked[1]["score"]
    assert "RAG" in ranked[0]["matched_interests"]
    assert "规则生成" in ranked[0]["recommendation_reason"]


def test_daily_api_persists_interests_results_and_read_state(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    calls = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return {
            "total_results": 3,
            "results": [
                _daily_result(
                    "2601.10001",
                    "Large Language Models with Retrieval Augmented Generation",
                    "This paper studies RAG for large language model systems.",
                ),
                _daily_result(
                    "2601.10002",
                    "Graph Knowledge Bases for AI Agents",
                    "Knowledge graphs support AI agent planning.",
                ),
                _daily_result(
                    "2601.10003",
                    "Network Traffic Measurement",
                    "A computer network measurement study.",
                ),
            ],
        }

    monkeypatch.setattr(
        daily_recommendation_service,
        "search_arxiv",
        fake_search,
    )
    saved = client.put(
        "/api/discovery/interests",
        json={
            "interests": [
                "RAG",
                "AI Agent",
                "计算机网络",
                "rag",
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["interests"] == [
        "RAG",
        "AI Agent",
        "计算机网络",
    ]

    first = client.post("/api/discovery/daily/refresh")
    assert first.status_code == 202, first.text
    state = client.get("/api/discovery/daily").json()["data"]
    assert state["current"]["status"] == "completed"
    assert len(state["current"]["items"]) == 3
    assert state["current"]["source"] == "arxiv_official_api"
    assert all(
        item["origin"] == "arxiv_external"
        and item["reason_source"] == "deterministic_keyword_rules"
        and item["summary"]
        and item["pdf_url"]
        for item in state["current"]["items"]
    )
    first_item = state["current"]["items"][0]
    marked = client.patch(
        f"/api/discovery/daily/items/{first_item['item_id']}/read",
        json={"is_read": True},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["data"]["is_read"] is True

    session_factory = isolated_environment["session_factory"]
    with session_factory() as db:
        batch = db.query(DailyRecommendationBatch).one()
        assert batch.recommendation_date == state["date"]
        assert batch.status == "completed"
        persisted_items = (
            db.query(DailyRecommendationItem)
            .filter(DailyRecommendationItem.batch_id == batch.id)
            .all()
        )
        assert len(persisted_items) == 3
        assert any(item.is_read for item in persisted_items)

    refreshed = client.post(
        "/api/discovery/daily/refresh",
        params={"force": True},
    )
    assert refreshed.status_code == 202, refreshed.text
    refreshed_state = client.get("/api/discovery/daily").json()["data"]
    assert len(refreshed_state["current"]["items"]) == 3
    assert refreshed_state["current"]["new_item_count"] == 0
    assert refreshed_state["current"]["items"][0]["is_read"] is True
    with session_factory() as db:
        assert db.query(DailyRecommendationItem).count() == 3
        assert db.query(DailyRecommendationItem).filter_by(is_read=True).count() == 1
    assert len(calls) >= 2


def test_daily_api_retains_clear_external_failure(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]

    def unavailable(**_kwargs):
        raise AppError(
            "arXiv 服务响应超时，请稍后重试。",
            code="ARXIV_TIMEOUT",
            status_code=504,
        )

    monkeypatch.setattr(
        daily_recommendation_service,
        "search_arxiv",
        unavailable,
    )
    response = client.post("/api/discovery/daily/refresh")
    assert response.status_code == 202, response.text
    state = client.get("/api/discovery/daily").json()["data"]
    assert state["current"]["status"] == "failed"
    assert state["current"]["error"] == "arXiv 服务响应超时，请稍后重试。"
    assert state["current"]["items"] == []
