from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.models.paper import Paper, PaperChunk


class ComparisonLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(self.payload, ensure_ascii=False)
                    )
                )
            ]
        )


def _add_paper_with_chunk(
    factory,
    *,
    title: str,
    text: str,
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
            authors="Alice Smith",
            publication_year=2024,
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


def _search_result(paper_id: int, chunk_id: int, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "paper_title": f"Paper {paper_id}",
        "page_number": 1,
        "text": text,
        "retrieval_method": "hybrid",
        "retrieval_score": 0.9,
        "retrieval_sources": ["bm25", "vector"],
        "bm25_rank": 1,
        "vector_rank": 1,
        "bm25_score": 1.0,
        "vector_score": 0.9,
    }


def _comparison_payload(
    first_id: int,
    second_id: int,
    *,
    methods_first_ids: list[int] | None = None,
) -> dict:
    methods_first_ids = methods_first_ids or [1]
    return {
        "title": "Structured Multi-paper Review",
        "sections": [
            {
                "heading": "综合比较",
                "content": "两篇论文的方法和实验证据存在差异。",
                "citation_ids": [1, 2],
            }
        ],
        "comparison": {
            "methods": [
                {
                    "paper_id": first_id,
                    "summary": "第一篇论文采用检索方法。",
                    "claim_kind": "evidence",
                    "citation_ids": methods_first_ids,
                },
                {
                    "paper_id": second_id,
                    "summary": "第二篇论文采用图方法。",
                    "claim_kind": "evidence",
                    "citation_ids": [2],
                },
            ],
            "results": [
                {
                    "paper_id": first_id,
                    "summary": "实验结果表明性能提升。",
                    "claim_kind": "evidence",
                    "citation_ids": [1],
                },
                {
                    "paper_id": second_id,
                    "summary": "实验结果表明准确率变化。",
                    "claim_kind": "evidence",
                    "citation_ids": [2],
                },
            ],
            "conclusions": [
                {
                    "paper_id": first_id,
                    "summary": "作者结论需要更多验证。",
                    "claim_kind": "inference",
                    "citation_ids": [1],
                },
                {
                    "paper_id": second_id,
                    "summary": "作者结论需要更多验证。",
                    "claim_kind": "inference",
                    "citation_ids": [2],
                },
            ],
            "limitations": [
                {
                    "paper_id": first_id,
                    "summary": "当前证据不足，无法确认局限性。",
                    "claim_kind": "evidence",
                    "citation_ids": [],
                },
                {
                    "paper_id": second_id,
                    "summary": "当前证据不足，无法确认局限性。",
                    "claim_kind": "evidence",
                    "citation_ids": [],
                },
            ],
        },
        "future_work": [],
    }


def test_review_builds_evidence_bound_four_dimension_comparison(
    isolated_environment,
    monkeypatch,
):
    import app.services.literature_review_service as review_service

    factory = isolated_environment["session_factory"]
    first_text = (
        "Method: a retrieval approach. Experiment results show higher "
        "accuracy. We conclude the model is effective."
    )
    second_text = (
        "Method: a graph model. Experiment results show better latency. "
        "We conclude graph models reduce latency. "
        "The study has a limitation in evaluation scale."
    )
    first_id, first_chunk = _add_paper_with_chunk(
        factory,
        title="Retrieval Method Paper",
        text=first_text,
    )
    second_id, second_chunk = _add_paper_with_chunk(
        factory,
        title="Graph Method Paper",
        text=second_text,
    )
    monkeypatch.setattr(
        review_service,
        "search_chunks_hybrid",
        lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                first_chunk if kwargs["paper_id"] == first_id else second_chunk,
                first_text if kwargs["paper_id"] == first_id else second_text,
            )
        ],
    )
    llm = ComparisonLLM(_comparison_payload(first_id, second_id))
    monkeypatch.setattr(review_service, "get_llm_client", lambda: llm)

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "method and results",
            "include_future_work": False,
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    comparisons = {
        item["dimension"]: item for item in data["dimension_comparison"]
    }
    assert set(comparisons) == {
        "methods",
        "results",
        "conclusions",
        "limitations",
    }
    for dimension in ("methods", "results", "conclusions"):
        findings = comparisons[dimension]["items"]
        assert {item["paper_id"] for item in findings} == {
            first_id,
            second_id,
        }
        assert all(item["citations"] for item in findings)
        assert all(
            citation["paper_id"] == item["paper_id"]
            for item in findings
            for citation in item["citations"]
        )
    assert all(
        item["claim_kind"] == "inference"
        for item in comparisons["conclusions"]["items"]
    )
    assert all(
        item["claim_kind"] == "insufficient_evidence"
        and item["evidence_status"] == "insufficient_evidence"
        for item in comparisons["limitations"]["items"]
    )
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "四维比较可用证据" in prompt
    assert "REVIEW" not in prompt


def test_review_rejects_cross_paper_evidence_attribution(
    isolated_environment,
    monkeypatch,
):
    import app.services.literature_review_service as review_service

    factory = isolated_environment["session_factory"]
    first_text = "Method and experimental results for paper one."
    second_text = "Method and experimental results for paper two."
    first_id, first_chunk = _add_paper_with_chunk(
        factory,
        title="First Attribution Paper",
        text=first_text,
    )
    second_id, second_chunk = _add_paper_with_chunk(
        factory,
        title="Second Attribution Paper",
        text=second_text,
    )
    monkeypatch.setattr(
        review_service,
        "search_chunks_hybrid",
        lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                first_chunk if kwargs["paper_id"] == first_id else second_chunk,
                first_text if kwargs["paper_id"] == first_id else second_text,
            )
        ],
    )
    payload = _comparison_payload(
        first_id,
        second_id,
        methods_first_ids=[2],
    )
    monkeypatch.setattr(
        review_service,
        "get_llm_client",
        lambda: ComparisonLLM(payload),
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "method attribution",
            "include_future_work": False,
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "REVIEW_EVIDENCE_ATTRIBUTION_ERROR"


def test_review_fills_missing_dimensions_as_insufficient(
    isolated_environment,
    monkeypatch,
):
    import app.services.literature_review_service as review_service

    factory = isolated_environment["session_factory"]
    first_id, first_chunk = _add_paper_with_chunk(
        factory,
        title="Partial Evidence One",
        text="Method only for first paper.",
    )
    second_id, second_chunk = _add_paper_with_chunk(
        factory,
        title="Partial Evidence Two",
        text="Method only for second paper.",
    )
    monkeypatch.setattr(
        review_service,
        "search_chunks_hybrid",
        lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                first_chunk if kwargs["paper_id"] == first_id else second_chunk,
                "Method only.",
            )
        ],
    )
    payload = {
        "title": "Partial Comparison",
        "sections": [
            {
                "heading": "Methods",
                "content": "Only method evidence is available.",
                "citation_ids": [1, 2],
            }
        ],
        "comparison": {"methods": []},
        "future_work": [],
    }
    monkeypatch.setattr(
        review_service,
        "get_llm_client",
        lambda: ComparisonLLM(payload),
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "partial evidence",
            "include_future_work": False,
        },
    )

    assert response.status_code == 200, response.text
    for dimension in response.json()["data"]["dimension_comparison"]:
        assert len(dimension["items"]) == 2
        assert all(
            item["evidence_status"] == "insufficient_evidence"
            for item in dimension["items"]
        )


def test_agent_chat_searches_selected_papers_independently(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api

    factory = isolated_environment["session_factory"]
    first_id, first_chunk = _add_paper_with_chunk(
        factory,
        title="Agent First Paper",
        text="First selected evidence.",
    )
    second_id, second_chunk = _add_paper_with_chunk(
        factory,
        title="Agent Second Paper",
        text="Second selected evidence.",
    )
    contexts_by_paper = {
        first_id: _search_result(first_id, first_chunk, "First selected evidence."),
        second_id: _search_result(second_id, second_chunk, "Second selected evidence."),
    }
    search = Mock(
        side_effect=lambda **kwargs: [contexts_by_paper[kwargs["paper_id"]]]
    )
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", search)
    monkeypatch.setattr(
        papers_api,
        "filter_relevant_contexts",
        lambda **kwargs: kwargs["contexts"],
    )
    generate = Mock(return_value="Evidence-bound answer.")
    monkeypatch.setattr(papers_api, "generate_rag_answer", generate)

    response = isolated_environment["client"].post(
        "/api/papers/agent/chat",
        params=[
            ("query", "compare methods"),
            ("paper_ids", str(first_id)),
            ("paper_ids", str(second_id)),
        ],
    )

    assert response.status_code == 200, response.text
    assert {call.kwargs["paper_id"] for call in search.call_args_list} == {
        first_id,
        second_id,
    }
    assert search.call_count == 2
    assert {
        context["paper_id"]
        for context in response.json()["data"]["contexts"]
    } == {first_id, second_id}


def test_agent_chat_rejects_missing_and_duplicate_selected_papers(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api

    paper_id, _ = _add_paper_with_chunk(
        isolated_environment["session_factory"],
        title="Selection Validation Paper",
        text="Selection evidence.",
    )
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", Mock(return_value=[]))
    client = isolated_environment["client"]

    duplicate = client.post(
        "/api/papers/agent/chat",
        params=[
            ("query", "compare"),
            ("paper_ids", str(paper_id)),
            ("paper_ids", str(paper_id)),
        ],
    )
    missing = client.post(
        "/api/papers/agent/chat",
        params=[
            ("query", "compare"),
            ("paper_ids", str(paper_id)),
            ("paper_ids", "999999"),
        ],
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "DUPLICATE_PAPER_ID"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PAPER_NOT_FOUND"


def test_agent_chat_refuses_when_selected_papers_have_no_evidence(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api

    factory = isolated_environment["session_factory"]
    first_id, _ = _add_paper_with_chunk(
        factory,
        title="No Evidence Agent One",
        text="Stored but not retrieved.",
    )
    second_id, _ = _add_paper_with_chunk(
        factory,
        title="No Evidence Agent Two",
        text="Stored but not retrieved either.",
    )
    monkeypatch.setattr(
        papers_api,
        "search_chunks_hybrid",
        Mock(return_value=[]),
    )
    generate = Mock()
    monkeypatch.setattr(papers_api, "generate_rag_answer", generate)

    response = isolated_environment["client"].post(
        "/api/papers/agent/chat",
        params=[
            ("query", "compare selected papers"),
            ("paper_ids", str(first_id)),
            ("paper_ids", str(second_id)),
        ],
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["contexts"] == []
    assert data["is_relevant"] is False
    assert "未找到" in data["answer"]
    generate.assert_not_called()
