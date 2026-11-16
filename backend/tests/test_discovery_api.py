from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.exceptions import AppError
from app.models.discovery import ExternalFavorite, ExternalPaper
from app.models.paper import Paper, PaperChunk, PaperImportItem, PaperImportJob
from app.services import arxiv_service, discovery_service


ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <published>2024-01-01T00:00:00Z</published>
    <updated>2024-01-02T00:00:00Z</updated>
    <title>Reliable Retrieval for Academic Discovery</title>
    <summary>This paper studies evidence-grounded literature discovery.</summary>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <arxiv:primary_category term="cs.IR" />
    <category term="cs.IR" />
    <link href="http://arxiv.org/abs/2401.00001v2"
          rel="alternate" type="text/html" />
    <link title="pdf" href="http://arxiv.org/pdf/2401.00001v2"
          rel="related" type="application/pdf" />
  </entry>
  <entry>
    <id>http://arxiv.org/abs/hep-th/9901001v1</id>
    <published>1999-01-01T00:00:00Z</published>
    <updated>1999-01-01T00:00:00Z</updated>
    <title>An Older arXiv Entry</title>
    <summary>Legacy identifier parsing test.</summary>
    <author><name>Carol Example</name></author>
    <arxiv:primary_category term="hep-th" />
    <category term="hep-th" />
    <link href="http://arxiv.org/abs/hep-th/9901001v1"
          rel="alternate" type="text/html" />
    <link title="pdf" href="http://arxiv.org/pdf/hep-th/9901001v1"
          rel="related" type="application/pdf" />
  </entry>
</feed>
"""


def _paper(
    factory,
    *,
    title: str,
    filename: str,
    text: str,
    file_hash: str | None = None,
) -> tuple[int, int]:
    digest = file_hash or hashlib.sha256(f"{title}-{uuid4()}".encode()).hexdigest()
    with factory() as db:
        paper = Paper(
            title=title,
            file_hash=digest,
            original_filename=filename,
            stored_filename=f"{uuid4()}.pdf",
            file_path=f"unused/{uuid4()}.pdf",
            file_size=max(len(text), 1),
            content_type="application/pdf",
            page_count=1,
            char_count=len(text),
            authors="Local Author",
            publication_year=2024,
            normalized_title=title.casefold(),
            parse_status="parsed",
            parse_checkpoint_version=1,
            parse_source_hash=digest,
            parse_source_size=max(len(text), 1),
            parse_source_page_count=1,
            index_status="indexed",
        )
        db.add(paper)
        db.flush()
        chunk = PaperChunk(
            paper_id=paper.id,
            page_number=1,
            page_chunk_index=0,
            chunk_index=0,
            text=text,
            content_type="paragraph",
        )
        db.add(chunk)
        db.commit()
        return paper.id, chunk.id


def _result(
    arxiv_id: str,
    title: str,
    *,
    summary: str = "Official arXiv metadata.",
) -> dict:
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": ["Alice Smith"],
        "summary": summary,
        "published": "2024-01-01T00:00:00+00:00",
        "updated": "2024-01-02T00:00:00+00:00",
        "primary_category": "cs.IR",
        "categories": ["cs.IR"],
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "origin": "arxiv_external",
    }


def test_arxiv_atom_parsing_preserves_real_metadata():
    parsed = arxiv_service.parse_arxiv_feed(ATOM_FEED)

    assert parsed["total_results"] == 2
    assert [item["arxiv_id"] for item in parsed["results"]] == [
        "2401.00001",
        "hep-th/9901001",
    ]
    first = parsed["results"][0]
    assert first["authors"] == ["Alice Smith", "Bob Jones"]
    assert first["title"] == "Reliable Retrieval for Academic Discovery"
    assert first["abs_url"].endswith("2401.00001v2")
    assert first["pdf_url"].endswith("2401.00001v2")


def test_search_api_forwards_pagination_and_sorts(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    local_id, _ = _paper(
        factory,
        title="Existing Local Paper",
        filename="2401.00001v1.pdf",
        text="local evidence",
    )
    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return {
            "query": kwargs["query"],
            "field": kwargs["field"],
            "page": kwargs["page"],
            "page_size": kwargs["page_size"],
            "sort_by": kwargs["sort_by"],
            "sort_order": kwargs["sort_order"],
            "total_results": 1,
            "total_pages": 1,
            "results": [_result("2401.00001", "External Result")],
            "source": "arxiv_official_api",
        }

    monkeypatch.setattr(discovery_service, "search_arxiv", fake_search)
    response = isolated_environment["client"].get(
        "/api/discovery/search",
        params={
            "query": "retrieval",
            "field": "title",
            "page": 2,
            "page_size": 5,
            "sort_by": "submitted",
            "sort_order": "ascending",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert captured == {
        "query": "retrieval",
        "field": "title",
        "page": 2,
        "page_size": 5,
        "sort_by": "submitted",
        "sort_order": "ascending",
    }
    assert data["source"] == "arxiv_official_api"
    assert data["results"][0]["origin"] == "arxiv_external"
    assert data["results"][0]["in_local_library"] is True
    assert data["results"][0]["local_paper_id"] == local_id


def test_empty_search_is_success(
    isolated_environment,
    monkeypatch,
):
    monkeypatch.setattr(
        discovery_service,
        "search_arxiv",
        lambda **kwargs: {
            **kwargs,
            "total_results": 0,
            "total_pages": 0,
            "results": [],
            "source": "arxiv_official_api",
        },
    )
    response = isolated_environment["client"].get(
        "/api/discovery/search",
        params={"query": "no matching paper"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["results"] == []


def test_upstream_timeout_and_rate_limit_are_preserved(
    isolated_environment,
    monkeypatch,
):
    for code, status in (
        ("ARXIV_TIMEOUT", 504),
        ("ARXIV_RATE_LIMITED", 429),
    ):
        monkeypatch.setattr(
            discovery_service,
            "search_arxiv",
            lambda _code=code, **_kwargs: (_ for _ in ()).throw(
                AppError("upstream error", code=_code, status_code=status)
            ),
        )
        response = isolated_environment["client"].get(
            "/api/discovery/search",
            params={"query": "network test"},
        )
        assert response.status_code == status
        assert response.json()["error"]["code"] == code


def test_favorite_rechecks_official_metadata_and_rejects_extra_fields(
    isolated_environment,
    monkeypatch,
):
    monkeypatch.setattr(
        discovery_service,
        "get_arxiv_paper",
        lambda _arxiv_id: _result(
            "2401.00001",
            "Official Metadata Title",
        ),
    )
    client = isolated_environment["client"]

    forged = client.post(
        "/api/discovery/favorites",
        json={
            "arxiv_id": "2401.00001",
            "title": "Client Forged Title",
        },
    )
    assert forged.status_code == 422

    response = client.post(
        "/api/discovery/favorites",
        json={"arxiv_id": "2401.00001"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["title"] == "Official Metadata Title"
    assert data["origin"] == "arxiv_external"


def test_import_uses_existing_hash_deduplication(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    pdf = b"%PDF-1.4\nlocal duplicate\n"
    digest = hashlib.sha256(pdf).hexdigest()
    local_id, _ = _paper(
        factory,
        title="Already Imported Paper",
        filename="downloaded.pdf",
        text="already present",
        file_hash=digest,
    )
    monkeypatch.setattr(
        discovery_service,
        "get_arxiv_paper",
        lambda _arxiv_id: _result("2401.00001", "Already Imported Paper"),
    )
    client = isolated_environment["client"]
    favorite = client.post(
        "/api/discovery/favorites",
        json={"arxiv_id": "2401.00001"},
    ).json()["data"]
    monkeypatch.setattr(
        discovery_service,
        "download_arxiv_pdf",
        lambda _url, _max_bytes: pdf,
    )

    response = client.post(
        f"/api/discovery/favorites/{favorite['favorite_id']}/import"
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "already_in_library"
    assert data["favorite"]["local_paper_id"] == local_id
    assert data["import_job"] is None


def test_pdf_download_failure_keeps_favorite_record(
    isolated_environment,
    monkeypatch,
):
    monkeypatch.setattr(
        discovery_service,
        "get_arxiv_paper",
        lambda _arxiv_id: _result("2401.00001", "Download Failure"),
    )
    client = isolated_environment["client"]
    favorite = client.post(
        "/api/discovery/favorites",
        json={"arxiv_id": "2401.00001"},
    ).json()["data"]
    monkeypatch.setattr(
        discovery_service,
        "download_arxiv_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AppError(
                "download failed",
                code="ARXIV_PDF_HTTP_ERROR",
                status_code=502,
            )
        ),
    )

    failed = client.post(
        f"/api/discovery/favorites/{favorite['favorite_id']}/import"
    )
    assert failed.status_code == 502
    favorites = client.get("/api/discovery/favorites").json()["data"]
    assert favorites[0]["arxiv_id"] == "2401.00001"
    assert favorites[0]["import_status"] == "failed"


def test_new_pdf_creates_existing_import_job(
    isolated_environment_no_worker,
    monkeypatch,
):
    environment = isolated_environment_no_worker
    client = environment["client"]
    monkeypatch.setattr(
        discovery_service,
        "get_arxiv_paper",
        lambda _arxiv_id: _result("2401.00001", "New Import"),
    )
    favorite = client.post(
        "/api/discovery/favorites",
        json={"arxiv_id": "2401.00001"},
    ).json()["data"]
    content = b"%PDF-1.4\nstaged discovery pdf\n"
    monkeypatch.setattr(
        discovery_service,
        "download_arxiv_pdf",
        lambda _url, _max_bytes: content,
    )

    response = client.post(
        f"/api/discovery/favorites/{favorite['favorite_id']}/import"
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["import_job"]["kind"] == "import"
    assert data["import_job"]["items"][0]["status"] == "queued"
    factory = environment["session_factory"]
    with factory() as db:
        staged_path = Path(
            db.query(PaperImportItem)
            .filter_by(id=data["import_job"]["items"][0]["id"])
            .one()
            .staged_path
        )
    assert staged_path.parent.name == ".imports"
    assert staged_path.read_bytes() == content


@pytest.mark.parametrize(
    ("item_status", "job_status", "expected_status", "expected_index_status"),
    [
        ("queued", "running", "queued", "pending"),
        ("running", "running", "running", "indexing"),
        ("succeeded", "completed", "imported", "indexed"),
        ("failed", "partial", "failed", "failed"),
    ],
)
def test_favorite_serializes_import_item_status_not_job_status(
    isolated_environment,
    monkeypatch,
    item_status,
    job_status,
    expected_status,
    expected_index_status,
):
    factory = isolated_environment["session_factory"]
    client = isolated_environment["client"]
    paper_id, _ = _paper(
        factory,
        title=f"Favorite Status {item_status}",
        filename=f"{item_status}.pdf",
        text="favorite status evidence",
    )
    monkeypatch.setattr(
        discovery_service,
        "get_arxiv_paper",
        lambda _arxiv_id: _result(
            "2401.00001",
            f"Favorite Status {item_status}",
        ),
    )
    favorite_payload = client.post(
        "/api/discovery/favorites",
        json={"arxiv_id": "2401.00001"},
    ).json()["data"]

    with factory() as db:
        favorite = db.query(ExternalFavorite).filter_by(
            id=favorite_payload["favorite_id"]
        ).one()
        job = PaperImportJob(
            kind="import",
            status=job_status,
            total_count=1,
            completed_count=1 if item_status == "succeeded" else 0,
            failed_count=1 if item_status == "failed" else 0,
        )
        db.add(job)
        db.flush()
        item = PaperImportItem(
            job_id=job.id,
            paper_id=paper_id,
            original_filename=f"{item_status}.pdf",
            staged_path=None,
            file_size=1,
            status=item_status,
            parse_status="parsed",
            index_status=expected_index_status,
            current_stage=(
                "completed"
                if item_status == "succeeded"
                else item_status
            ),
            progress=100 if item_status == "succeeded" else 0,
            error_message=(
                "simulated failure"
                if item_status == "failed"
                else None
            ),
        )
        db.add(item)
        db.flush()
        favorite.import_job_id = job.id
        favorite.import_item_id = item.id
        favorite.import_status = "queued"
        db.commit()

    favorites = client.get("/api/discovery/favorites")
    assert favorites.status_code == 200, favorites.text
    favorite = next(
        item
        for item in favorites.json()["data"]
        if item["favorite_id"] == favorite_payload["favorite_id"]
    )
    assert favorite["import_status"] == expected_status
    assert favorite["import_job"]["status"] == job_status
    assert favorite["import_job"]["items"][0]["status"] == item_status
    if item_status == "succeeded":
        assert favorite["local_paper_id"] == paper_id
    if item_status == "failed":
        assert favorite["import_error"] == "simulated failure"


def test_tracking_history_counts_only_new_items(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    tracking = client.post(
        "/api/discovery/tracking",
        json={
            "name": "Retrieval watch",
            "query": "retrieval",
            "field": "all",
            "sort_by": "submitted",
            "sort_order": "descending",
            "page_size": 10,
        },
    ).json()["data"]
    payloads = [
        [
            _result("2401.00001", "One"),
            _result("2401.00002", "Two"),
        ],
        [
            _result("2401.00002", "Two"),
            _result("2401.00003", "Three"),
        ],
    ]

    def fake_search(**_kwargs):
        results = payloads.pop(0)
        return {
            "results": results,
            "total_results": len(results),
        }

    monkeypatch.setattr(discovery_service, "search_arxiv", fake_search)
    first = client.post(
        f"/api/discovery/tracking/{tracking['tracking_id']}/refresh"
    )
    second = client.post(
        f"/api/discovery/tracking/{tracking['tracking_id']}/refresh"
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["latest_run"]["new_result_count"] == 2
    assert second.json()["data"]["latest_run"]["new_result_count"] == 1
    assert len(second.json()["data"]["history"]) == 2


def test_tracking_failure_is_retained(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    tracking = client.post(
        "/api/discovery/tracking",
        json={"name": "Failure watch", "query": "failure"},
    ).json()["data"]
    monkeypatch.setattr(
        discovery_service,
        "search_arxiv",
        lambda **_kwargs: (_ for _ in ()).throw(
            AppError(
                "network down",
                code="ARXIV_NETWORK_ERROR",
                status_code=502,
            )
        ),
    )

    failed = client.post(
        f"/api/discovery/tracking/{tracking['tracking_id']}/refresh"
    )
    assert failed.status_code == 502
    history = client.get(
        f"/api/discovery/tracking/{tracking['tracking_id']}"
    ).json()["data"]["history"]
    assert history[0]["status"] == "failed"
    assert history[0]["error_message"] == "network down"


def test_recommendations_distinguish_external_and_local_sources(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    paper_id, chunk_id = _paper(
        factory,
        title="Local Retrieval Evidence",
        filename="local.pdf",
        text="Local evidence about retrieval.",
    )
    monkeypatch.setattr(
        discovery_service,
        "get_arxiv_paper",
        lambda _arxiv_id: _result("2401.00001", "Favorite Seed"),
    )
    client = isolated_environment["client"]
    client.post(
        "/api/discovery/favorites",
        json={"arxiv_id": "2401.00001"},
    )
    monkeypatch.setattr(
        discovery_service,
        "search_arxiv",
        lambda **kwargs: {
            "results": [
                _result("2401.00009", "External Recommendation")
            ],
            "total_results": 1,
        },
    )
    monkeypatch.setattr(
        discovery_service,
        "search_chunks_hybrid",
        lambda **_kwargs: [
            {
                "paper_id": paper_id,
                "page_number": 1,
                "chunk_id": chunk_id,
                "text": "Local evidence about retrieval.",
            }
        ],
    )

    response = client.get("/api/discovery/recommendations")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["external_results"][0]["origin"] == "arxiv_external"
    assert data["local_recommendations"][0]["origin"] == "local_library"
    assert data["local_recommendations"][0]["paper_id"] == paper_id
    assert data["local_rag_status"] == "available"
