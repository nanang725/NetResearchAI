from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.models.review import LiteratureReviewRecord
from app.services import review_record_service


def test_review_session_persists_configuration_and_result(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]

    def fake_review(_db, *, paper_ids, topic, include_future_work):
        assert paper_ids == [11, 12]
        assert topic == "持久化综述主题"
        assert include_future_work is False
        return {
            "title": "持久化多文献综述",
            "topic": topic,
            "selected_paper_ids": paper_ids,
            "sections": [],
            "paper_coverage": [],
            "dimension_comparison": [],
            "future_work": [],
            "sources": [],
            "model": "test-model",
            "disclaimer": "test",
        }

    monkeypatch.setattr(
        review_record_service,
        "generate_literature_review",
        fake_review,
    )
    created = client.post(
        "/api/reviews/sessions",
        json={
            "paper_ids": [11, 12],
            "topic": "持久化综述主题",
            "include_future_work": False,
        },
    )
    assert created.status_code == 202, created.text
    review_id = created.json()["data"]["review_id"]

    loaded = client.get(f"/api/reviews/sessions/{review_id}")
    assert loaded.status_code == 200, loaded.text
    data = loaded.json()["data"]
    assert data["status"] == "completed"
    assert data["selected_paper_ids"] == [11, 12]
    assert data["topic"] == "持久化综述主题"
    assert data["include_future_work"] is False
    assert data["result"]["title"] == "持久化多文献综述"

    latest = client.get("/api/reviews/sessions/latest")
    assert latest.status_code == 200, latest.text
    assert latest.json()["data"]["review_id"] == review_id


def test_empty_latest_review_session_is_null(isolated_environment):
    client = isolated_environment["client"]

    latest = client.get("/api/reviews/sessions/latest")

    assert latest.status_code == 200, latest.text
    assert latest.json() == {
        "success": True,
        "data": None,
        "error": None,
    }


def test_interrupted_review_record_is_recovered_as_retryable_failure(
    isolated_environment,
):
    client = isolated_environment["client"]
    session_factory = isolated_environment["session_factory"]
    review_id = str(uuid4())
    with session_factory() as db:
        db.add(
            LiteratureReviewRecord(
                id=review_id,
                selected_paper_ids_json="[31,32]",
                topic="interrupted review",
                include_future_work=True,
                status="running",
                started_at=datetime.utcnow(),
            )
        )
        db.commit()
        assert review_record_service.recover_interrupted_review_records(db) == 1

    restored = client.get(f"/api/reviews/sessions/{review_id}")
    assert restored.status_code == 200, restored.text
    data = restored.json()["data"]
    assert data["status"] == "failed"
    assert data["selected_paper_ids"] == [31, 32]
    assert data["topic"] == "interrupted review"
    assert "重启" in data["error"]


def test_review_session_exposes_failure_and_retry(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    calls = {"count": 0}

    def flaky_review(_db, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated review failure")
        return {
            "title": "Retried Review",
            "topic": "retry",
            "selected_paper_ids": [21, 22],
            "sections": [],
            "paper_coverage": [],
            "dimension_comparison": [],
            "future_work": [],
            "sources": [],
            "model": "test-model",
            "disclaimer": "test",
        }

    monkeypatch.setattr(
        review_record_service,
        "generate_literature_review",
        flaky_review,
    )
    created = client.post(
        "/api/reviews/sessions",
        json={
            "paper_ids": [21, 22],
            "topic": "retry review",
            "include_future_work": True,
        },
    )
    review_id = created.json()["data"]["review_id"]
    failed = client.get(f"/api/reviews/sessions/{review_id}").json()["data"]
    assert failed["status"] == "failed"
    assert "simulated review failure" in failed["error"]

    retried = client.post(f"/api/reviews/sessions/{review_id}/retry")
    assert retried.status_code == 202, retried.text
    completed = client.get(f"/api/reviews/sessions/{review_id}").json()["data"]
    assert completed["status"] == "completed"
    assert completed["result"]["title"] == "Retried Review"
