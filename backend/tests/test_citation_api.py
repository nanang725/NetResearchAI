from __future__ import annotations

import hashlib
from uuid import uuid4

from app.models.paper import Paper, PaperElement, PaperPage
from app.services.citation_service import normalize_title


def _add_paper(db, title: str, *, doi: str | None = None, year: int = 2024):
    digest = hashlib.sha256(f"{title}-{uuid4()}".encode()).hexdigest()
    paper = Paper(
        title=title,
        file_hash=digest,
        original_filename=f"{title}.pdf",
        stored_filename=f"{uuid4()}.pdf",
        file_path=f"unused/{digest}.pdf",
        file_size=1,
        content_type="application/pdf",
        page_count=2,
        char_count=100,
        authors="Alice Smith",
        doi=doi,
        normalized_title=normalize_title(title),
        publication_year=year,
        parse_status="parsed",
        parse_checkpoint_version=1,
        parse_source_hash=digest,
        parse_source_size=1,
        parse_source_page_count=2,
        index_status="indexed",
    )
    db.add(paper)
    db.flush()
    return paper


def _add_reference(db, source: Paper, text: str):
    page = PaperPage(
        paper_id=source.id,
        page_number=2,
        text=text,
        char_count=len(text),
        parse_status="text_extracted",
        checkpoint_status="completed",
    )
    db.add(page)
    db.flush()
    element = PaperElement(
        paper_id=source.id,
        page_id=page.id,
        page_number=2,
        element_index=0,
        element_type="reference",
        text=text,
        extraction_status="heuristic",
    )
    db.add(element)
    db.flush()


def test_citation_rebuild_graph_and_paper_detail_api(isolated_environment):
    factory = isolated_environment["session_factory"]
    client = isolated_environment["client"]
    with factory() as db:
        source = _add_paper(db, "Calling Paper")
        target = _add_paper(
            db,
            "Core Routing Paper",
            doi="10.1000/core",
        )
        _add_reference(
            db,
            source,
            "[1] Alice Smith. Core Routing Paper. 2024. doi:10.1000/core",
        )
        db.commit()
        source_id, target_id = source.id, target.id

    rebuild = client.post("/api/citations/rebuild", json={})
    assert rebuild.status_code == 200, rebuild.text
    assert rebuild.json()["data"]["edge_count"] == 1
    assert rebuild.json()["data"]["rebuilt_paper_ids"] == [source_id, target_id]

    second = client.post("/api/citations/rebuild", json={})
    assert second.status_code == 200
    assert second.json()["data"]["edge_count"] == 1
    assert second.json()["data"]["reference_count"] == 1

    graph = client.get("/api/citations/graph")
    assert graph.status_code == 200
    graph_data = graph.json()["data"]
    assert graph_data["stats"]["paper_count"] == 2
    assert graph_data["stats"]["edge_count"] == 1
    assert graph_data["stats"]["matched_reference_count"] == 1
    assert len(graph_data["nodes"]) == 2
    assert len(graph_data["edges"]) == 1
    edge = graph_data["edges"][0]
    assert edge["source"] == str(source_id)
    assert edge["target"] == str(target_id)
    assert edge["match_method"] == "doi"
    target_node = next(
        node for node in graph_data["nodes"] if node["paper_id"] == target_id
    )
    assert target_node["in_degree"] == 1
    assert target_node["pagerank"] > 0
    assert target_node["is_foundational"] is True

    detail = client.get(f"/api/citations/papers/{target_id}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert detail_data["in_degree"] == 1
    assert detail_data["outgoing"] == []
    assert detail_data["incoming"][0]["paper_id"] == source_id
    assert detail_data["incoming"][0]["source_page"] == 2
    assert "10.1000/core" in detail_data["incoming"][0]["raw_reference"]

    source_detail = client.get(f"/api/citations/papers/{source_id}")
    assert source_detail.status_code == 200
    assert source_detail.json()["data"]["outgoing"][0]["paper_id"] == target_id


def test_citation_graph_empty_and_filters(isolated_environment):
    client = isolated_environment["client"]
    graph = client.get("/api/citations/graph")
    assert graph.status_code == 200
    data = graph.json()["data"]
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["stats"]["paper_count"] == 0

    factory = isolated_environment["session_factory"]
    with factory() as db:
        _add_paper(db, "Isolated Paper", year=2023)
        db.commit()

    isolated = client.get(
        "/api/citations/graph",
        params={"include_isolated": "true"},
    )
    assert isolated.status_code == 200
    assert len(isolated.json()["data"]["nodes"]) == 1
    filtered = client.get(
        "/api/citations/graph",
        params={"include_isolated": "false"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["data"]["nodes"] == []
    assert filtered.json()["data"]["stats"]["paper_count"] == 1

    invalid_rebuild = client.post(
        "/api/citations/rebuild",
        json={"paper_ids": []},
    )
    assert invalid_rebuild.status_code == 422


def test_single_rebuild_and_not_found_errors(isolated_environment):
    factory = isolated_environment["session_factory"]
    client = isolated_environment["client"]
    with factory() as db:
        paper = _add_paper(db, "Single Rebuild Paper")
        _add_reference(
            db,
            paper,
            "Alice Smith. Unknown External Work. 2022.",
        )
        db.commit()
        paper_id = paper.id

    single = client.post(f"/api/citations/papers/{paper_id}/rebuild")
    assert single.status_code == 200
    assert single.json()["data"]["rebuilt_paper_ids"] == [paper_id]
    assert single.json()["data"]["unmatched_reference_count"] == 1

    missing_detail = client.get("/api/citations/papers/999999")
    assert missing_detail.status_code == 404
    assert missing_detail.json()["error"]["code"] == "PAPER_NOT_FOUND"

    missing_rebuild = client.post("/api/citations/papers/999999/rebuild")
    assert missing_rebuild.status_code == 404
    assert missing_rebuild.json()["error"]["code"] == "PAPER_NOT_FOUND"
