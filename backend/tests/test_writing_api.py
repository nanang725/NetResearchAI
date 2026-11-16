from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.services import academic_writing_service, translation_service


class WritingTranslationLLM:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        match = re.search(
            r"【待翻译原文】\n(.*?)\n\n【翻译要求】",
            prompt,
            re.DOTALL,
        )
        assert match is not None
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f"Translated: {match.group(1)}"
                    )
                )
            ]
        )


def _add_writing_paper(
    factory,
    *,
    title: str,
    text: str,
    authors: str | None = "Alice Smith",
    year: int | None = 2024,
    venue: str | None = None,
    doi: str | None = None,
) -> tuple[int, int]:
    digest = hashlib.sha256(f"{title}-{uuid4()}".encode()).hexdigest()
    with factory() as db:
        paper = Paper(
            title=title,
            file_hash=digest,
            original_filename=f"{title}.pdf",
            stored_filename=f"{uuid4()}.pdf",
            file_path=f"unused/{digest}.pdf",
            file_size=1,
            content_type="application/pdf",
            page_count=1,
            char_count=len(text),
            authors=authors,
            publication_year=year,
            venue=venue,
            doi=doi,
            normalized_title=title.casefold(),
            parse_status="parsed",
            parse_checkpoint_version=1,
            parse_source_hash=digest,
            parse_source_size=1,
            parse_source_page_count=1,
            index_status="indexed",
        )
        db.add(paper)
        db.flush()
        page = PaperPage(
            paper_id=paper.id,
            page_number=1,
            text=text,
            char_count=len(text),
            parse_status="text_extracted",
            checkpoint_status="completed",
        )
        db.add(page)
        db.flush()
        element = PaperElement(
            paper_id=paper.id,
            page_id=page.id,
            page_number=1,
            element_index=0,
            element_type="paragraph",
            text=text,
            section_path=json.dumps(["Introduction", "Research problem"]),
            extraction_status="extracted",
        )
        db.add(element)
        db.flush()
        chunk = PaperChunk(
            paper_id=paper.id,
            page_number=1,
            page_chunk_index=0,
            chunk_index=0,
            text=text,
            content_type="paragraph",
            element_id=element.id,
        )
        db.add(chunk)
        db.commit()
        return paper.id, chunk.id


def _search_result(paper_id: int, chunk_id: int, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "paper_title": "ignored",
        "page_number": 1,
        "page_chunk_index": 0,
        "text": text,
        "retrieval_method": "hybrid",
        "retrieval_score": 0.9,
        "retrieval_sources": ["bm25", "vector"],
    }


def test_outline_uses_real_chunks_and_marks_unsupported_section(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    text = "The paper defines a routing problem and evaluates packet delivery."
    paper_id, chunk_id = _add_writing_paper(
        factory,
        title="Evidence Grounded Routing",
        text=text,
    )
    search = Mock(
        return_value=[_search_result(paper_id, chunk_id, "fabricated text")]
    )
    monkeypatch.setattr(
        academic_writing_service,
        "search_chunks_hybrid",
        search,
    )

    response = isolated_environment["client"].post(
        "/api/writing/outline",
        json={"paper_id": paper_id, "topic": "routing reliability"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["evidence_sources"][0]["chunk_id"] == chunk_id
    assert data["evidence_sources"][0]["excerpt"] == text
    assert data["evidence_sources"][0]["excerpt"] != "fabricated text"
    assert any(
        section["evidence_status"] == "insufficient_evidence"
        and section["evidence"] == []
        for section in data["sections"]
    )
    search.assert_called_once()
    assert search.call_args.kwargs["paper_id"] == paper_id


def test_outline_without_evidence_returns_422(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    paper_id, _ = _add_writing_paper(
        factory,
        title="No Evidence Paper",
        text="Stored content without a retrieval hit.",
    )
    monkeypatch.setattr(
        academic_writing_service,
        "search_chunks_hybrid",
        lambda **_kwargs: [],
    )

    response = isolated_environment["client"].post(
        "/api/writing/outline",
        json={"paper_id": paper_id, "topic": "missing evidence"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OUTLINE_INSUFFICIENT_EVIDENCE"


def test_references_copy_only_local_verified_metadata(
    isolated_environment,
):
    factory = isolated_environment["session_factory"]
    verified_id, chunk_id = _add_writing_paper(
        factory,
        title="Verified Local Paper",
        text="Verified local evidence text.",
        authors="Alice Smith, Bob Jones",
        year=2024,
        venue="Local Conference",
        doi="10.1000/real",
    )
    unknown_id, _ = _add_writing_paper(
        factory,
        title="Unknown Metadata Paper",
        text="Unknown metadata evidence.",
        authors="未知作者",
        year=None,
        venue=None,
        doi=None,
    )

    response = isolated_environment["client"].post(
        "/api/writing/references",
        json={"paper_ids": [verified_id, unknown_id]},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    references = {item["paper_id"]: item for item in data["references"]}
    verified = references[verified_id]
    unknown = references[unknown_id]
    assert verified["doi"] == "10.1000/real"
    assert verified["authors"] == "Alice Smith, Bob Jones"
    assert verified["year"] == 2024
    assert verified["evidence"]["chunk_id"] == chunk_id
    assert verified["can_cite"] is True
    assert unknown["doi"] is None
    assert unknown["authors"] is None
    assert unknown["year"] is None
    assert set(unknown["unknown_fields"]) >= {"authors", "year", "doi", "venue"}
    assert "10.1000/real" not in unknown["citation_text"]
    assert data["verified_count"] == 2


def test_references_empty_library_returns_empty_list(
    isolated_environment,
):
    response = isolated_environment["client"].post(
        "/api/writing/references",
        json={"paper_ids": []},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["references"] == []
    assert data["total_count"] == 0
    assert data["verified_count"] == 0


def test_csv_chart_uses_exact_uploaded_aggregates(
    isolated_environment,
):
    content = (
        "category,value\n"
        "Alpha,1\n"
        "Alpha,3\n"
        "Beta,8\n"
        "Beta,10\n"
    ).encode("utf-8")
    response = isolated_environment["client"].post(
        "/api/datasets/analyze",
        files={"file": ("metrics.csv", content, "text/csv")},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    chart = data["chart"]
    assert data["row_count"] == 4
    assert data["missing_cell_count"] == 0
    assert chart["categories"] == ["Beta", "Alpha"]
    assert chart["values"] == [9.0, 2.0]
    assert chart["aggregation"] == "mean"
    assert chart["data_origin"] == "uploaded_csv_aggregation"
    assert "真实" in data["disclaimer"] or "上传" in data["disclaimer"]


def test_csv_rejects_empty_header_only_and_bad_rows(
    isolated_environment,
):
    client = isolated_environment["client"]
    empty = client.post(
        "/api/datasets/analyze",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    header_only = client.post(
        "/api/datasets/analyze",
        files={"file": ("header.csv", b"a,b\n", "text/csv")},
    )
    bad_rows = client.post(
        "/api/datasets/analyze",
        files={
            "file": (
                "bad.csv",
                b"a,b\n1,2\n3\n",
                "text/csv",
            )
        },
    )

    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "CSV_EMPTY"
    assert header_only.status_code == 422
    assert header_only.json()["error"]["code"] == "CSV_NO_DATA_ROWS"
    assert bad_rows.status_code == 400
    assert bad_rows.json()["error"]["code"] == "CSV_ROW_LENGTH_MISMATCH"


def test_academic_translation_reuses_protection(
    isolated_environment,
    monkeypatch,
):
    llm = WritingTranslationLLM()
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: llm,
    )
    text = "系统依据证据生成大纲，保留引用 [1] 与 DOI 10.1000/xyz。"

    response = isolated_environment["client"].post(
        "/api/writing/translate",
        json={"text": text, "direction": "zh_to_en"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "[1]" in data["translated_text"]
    assert "10.1000/xyz" in data["translated_text"]
    assert data["protected_token_count"] >= 2
    assert len(llm.calls) == 1
