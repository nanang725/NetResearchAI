from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk
from app.services import literature_review_service


class ReviewLLM:
    def __init__(self, payload: dict | None = None, *, fail: bool = False):
        self.payload = payload or {}
        self.fail = fail
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    def create(self, **kwargs):
        if self.fail:
            raise TimeoutError("simulated review timeout")
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


class SequenceReviewLLM:
    def __init__(
        self,
        contents: list[str],
        finish_reasons: list[str] | None = None,
    ):
        self.contents = contents
        self.finish_reasons = finish_reasons or []
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.contents) - 1)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.contents[index]),
                    finish_reason=(
                        self.finish_reasons[index]
                        if index < len(self.finish_reasons)
                        else "stop"
                    ),
                )
            ]
        )


def test_review_json_repair_is_bounded_and_preserves_schema(
    monkeypatch,
):
    valid = json.dumps(
        {
            "title": "Repaired Review",
            "sections": [
                {
                    "heading": "Evidence",
                    "content": "A grounded comparison.",
                    "citation_ids": [1],
                }
            ],
            "comparison": {},
            "future_work": [],
        }
    )
    llm = SequenceReviewLLM(['{"title": "truncated"', valid])
    consume = Mock()
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )

    parsed, _model = literature_review_service._call_review_model_bounded(
        "Build an evidence review.",
        consume_llm_call=consume,
    )

    assert parsed["title"] == "Repaired Review"
    assert parsed["_llm_diagnostics"][0]["failure_kind"] == "json_parse_error"
    assert len(llm.calls) == 2
    assert consume.call_count == 2
    assert all(
        call["response_format"] == {"type": "json_object"}
        for call in llm.calls
    )
    assert "Repair the supplied JSON only" in (
        llm.calls[1]["messages"][0]["content"]
    )


def test_review_schema_failure_uses_one_repair_request(monkeypatch):
    invalid_schema = json.dumps(
        {
            "title": "Missing section content",
            "sections": [{"heading": "Evidence", "citation_ids": [1]}],
        }
    )
    valid = json.dumps(
        {
            "title": "Schema Repaired",
            "sections": [
                {
                    "heading": "Evidence",
                    "content": "The section is complete.",
                    "citation_ids": [1],
                }
            ],
            "comparison": {},
            "future_work": [],
        }
    )
    llm = SequenceReviewLLM([invalid_schema, valid])
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )

    parsed, _model = literature_review_service._call_review_model_bounded(
        "Build an evidence review.",
    )

    assert parsed["title"] == "Schema Repaired"
    assert len(llm.calls) == 2


def test_truncated_review_uses_compact_repair_prompt(monkeypatch):
    valid = json.dumps(
        {
            "title": "Compact Review",
            "sections": [
                {
                    "heading": "Evidence",
                    "content": "Compact grounded content.",
                    "citation_ids": [1],
                }
            ],
            "comparison": {},
            "future_work": [],
        }
    )
    llm = SequenceReviewLLM(
        ['{"title":"truncated', valid],
        finish_reasons=["length", "stop"],
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )

    parsed, _model = literature_review_service._call_review_model_bounded(
        "Original evidence prompt",
    )

    assert parsed["title"] == "Compact Review"
    assert parsed["_llm_diagnostics"][0]["failure_kind"] == "output_truncated"
    assert "compact complete object" in (
        llm.calls[1]["messages"][1]["content"]
    )


def test_review_two_invalid_json_responses_raise_sanitized_diagnostic(
    monkeypatch,
):
    llm = SequenceReviewLLM(
        ['{"secret": "provider text is not valid', "still not json"]
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )

    with pytest.raises(AppError) as captured:
        literature_review_service._call_review_model_bounded("review")

    assert captured.value.code == "REVIEW_LLM_JSON_ERROR"
    details = captured.value.details
    assert details["attempts"] == 2
    assert len(details["diagnostics"]) == 2
    serialized = json.dumps(details)
    assert "provider text is not valid" not in serialized
    assert all(
        item["failure_kind"] == "json_parse_error"
        for item in details["diagnostics"]
    )


def test_review_empty_response_is_distinct_from_json_or_schema_failure(
    monkeypatch,
):
    llm = SequenceReviewLLM(["", ""])
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )

    with pytest.raises(AppError) as captured:
        literature_review_service._call_review_model_bounded("review")

    assert captured.value.code == "REVIEW_LLM_RESPONSE_ERROR"
    assert all(
        item["failure_kind"] == "empty_response"
        for item in captured.value.details["diagnostics"]
    )


def _add_paper_with_chunks(
    factory,
    title: str,
    chunks: list[str],
) -> tuple[int, list[int]]:
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
            page_count=len(chunks),
            char_count=sum(len(value) for value in chunks),
            authors="Alice Smith",
            publication_year=2024,
            normalized_title=title.casefold(),
            parse_status="parsed",
            parse_checkpoint_version=1,
            parse_source_hash=digest,
            parse_source_size=1,
            parse_source_page_count=len(chunks),
            index_status="indexed",
        )
        db.add(paper)
        db.flush()
        chunk_ids = []
        for index, text in enumerate(chunks, start=1):
            chunk = PaperChunk(
                paper_id=paper.id,
                page_number=index,
                page_chunk_index=0,
                chunk_index=index - 1,
                text=text,
                content_type="paragraph",
            )
            db.add(chunk)
            db.flush()
            chunk_ids.append(chunk.id)
        db.commit()
        return paper.id, chunk_ids


def _search_result(paper_id: int, chunk_id: int, page: int, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "paper_title": f"Paper {paper_id}",
        "page_number": page,
        "text": text,
        "retrieval_method": "hybrid",
        "retrieval_score": 0.9,
        "retrieval_sources": ["bm25", "vector"],
        "bm25_rank": 1,
        "vector_rank": 1,
        "bm25_score": 1.0,
        "vector_score": 0.9,
    }


def _valid_review_payload() -> dict:
    return {
        "title": "Evidence-Grounded Network Review",
        "sections": [
            {
                "heading": "研究现状",
                "content": "两篇论文分别提出不同但互补的网络方法。",
                "citation_ids": [1, 2],
            }
        ],
        "future_work": [],
    }


def test_review_searches_each_selected_paper_independently(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_id, first_chunks = _add_paper_with_chunks(
        factory,
        "First Review Paper",
        ["First paper routing evidence."],
    )
    second_id, second_chunks = _add_paper_with_chunks(
        factory,
        "Second Review Paper",
        ["Second paper routing evidence."],
    )
    search = Mock(
        side_effect=lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                first_chunks[0]
                if kwargs["paper_id"] == first_id
                else second_chunks[0],
                1,
                "Selected paper evidence.",
            )
        ]
    )
    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        search,
    )
    llm = ReviewLLM(_valid_review_payload())
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "路由方法比较",
            "include_future_work": True,
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert search.call_count == 2
    assert {
        call.kwargs["paper_id"]
        for call in search.call_args_list
    } == {first_id, second_id}
    assert all(call.kwargs["limit"] == 4 for call in search.call_args_list)
    assert {item["paper_id"] for item in data["sources"]} == {
        first_id,
        second_id,
    }
    assert all(
        item["status"] == "covered"
        for item in data["paper_coverage"]
    )
    assert "AI" in data["disclaimer"]
    assert len(llm.calls) == 1
    assert "路由方法比较" in llm.calls[0]["messages"][-1]["content"]


def test_review_five_papers_and_selection_validation(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    papers = [
        _add_paper_with_chunks(
            factory,
            f"Review Paper {index}",
            [f"Evidence paragraph {index} about reliable networks."],
        )
        for index in range(1, 6)
    ]
    search = Mock(
        side_effect=lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                next(
                    chunk_id
                    for paper_id, chunk_ids in papers
                    if paper_id == kwargs["paper_id"]
                    for chunk_id in chunk_ids
                ),
                1,
                "Independent evidence.",
            )
        ]
    )
    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        search,
    )
    llm = ReviewLLM(
        {
            "title": "Five Paper Review",
            "sections": [
                {
                    "heading": "综合分析",
                    "content": "五篇论文提供了独立证据。",
                    "citation_ids": [1, 2, 3, 4, 5],
                }
            ],
            "future_work": [],
        }
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )
    client = isolated_environment["client"]

    response = client.post(
        "/api/reviews",
        json={
            "paper_ids": [paper_id for paper_id, _ in papers],
            "topic": "五篇论文比较",
            "include_future_work": False,
        },
    )
    assert response.status_code == 200, response.text
    assert search.call_count == 5
    assert len(response.json()["data"]["paper_coverage"]) == 5

    too_few = client.post(
        "/api/reviews",
        json={
            "paper_ids": [papers[0][0]],
            "topic": "Only one",
            "include_future_work": False,
        },
    )
    assert too_few.status_code == 422

    too_many = client.post(
        "/api/reviews",
        json={
            "paper_ids": [paper_id for paper_id, _ in papers] + [papers[0][0]],
            "topic": "Too many",
            "include_future_work": False,
        },
    )
    assert too_many.status_code == 422

    duplicate = client.post(
        "/api/reviews",
        json={
            "paper_ids": [papers[0][0], papers[0][0]],
            "topic": "Duplicate papers",
            "include_future_work": False,
        },
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "DUPLICATE_PAPER_ID"

    missing = client.post(
        "/api/reviews",
        json={
            "paper_ids": [papers[0][0], 999999],
            "topic": "Missing paper",
            "include_future_work": False,
        },
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PAPER_NOT_FOUND"


def test_review_marks_insufficient_paper_without_cross_paper_evidence(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_id, first_chunks = _add_paper_with_chunks(
        factory,
        "Covered Review Paper",
        ["Covered evidence."],
    )
    second_id, _ = _add_paper_with_chunks(
        factory,
        "Insufficient Review Paper",
        ["Unrelated text."],
    )

    def search(**kwargs):
        if kwargs["paper_id"] == first_id:
            return [
                _search_result(
                    first_id,
                    first_chunks[0],
                    1,
                    "Covered evidence.",
                )
            ]
        return []

    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        search,
    )
    captured_prompt = {}
    llm = ReviewLLM(
        {
            "title": "Partial Evidence Review",
            "sections": [
                {
                    "heading": "Evidence coverage",
                    "content": "第二篇论文证据不足。",
                    "citation_ids": [1],
                }
            ],
            "future_work": [],
        }
    )

    original_create = llm.chat.completions.create

    def create(**kwargs):
        captured_prompt["prompt"] = kwargs["messages"][-1]["content"]
        return original_create(**kwargs)

    llm.chat.completions.create = create
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: llm,
    )
    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "evidence",
            "include_future_work": False,
        },
    )

    assert response.status_code == 200, response.text
    coverage = {
        item["paper_id"]: item
        for item in response.json()["data"]["paper_coverage"]
    }
    assert coverage[first_id]["status"] == "covered"
    assert coverage[second_id]["status"] == "insufficient_evidence"
    assert "insufficient_evidence" in captured_prompt["prompt"]
    assert all(
        citation["paper_id"] == first_id
        for section in response.json()["data"]["sections"]
        for citation in section["citations"]
    )


def test_invalid_review_citation_is_rejected(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_id, first_chunks = _add_paper_with_chunks(
        factory,
        "Invalid Citation First",
        ["First evidence."],
    )
    second_id, second_chunks = _add_paper_with_chunks(
        factory,
        "Invalid Citation Second",
        ["Second evidence."],
    )
    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                first_chunks[0]
                if kwargs["paper_id"] == first_id
                else second_chunks[0],
                1,
                "Valid evidence.",
            )
        ],
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: ReviewLLM(
            {
                "title": "Invalid",
                "sections": [
                    {
                        "heading": "Bad citation",
                        "content": "Unsupported.",
                        "citation_ids": [999],
                    }
                ],
                "future_work": [],
            }
        ),
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "invalid",
            "include_future_work": False,
        },
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "REVIEW_INVALID_CITATION"


def test_review_requires_evidence_and_does_not_modify_chunks(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_id, _ = _add_paper_with_chunks(
        factory,
        "No Evidence First",
        ["Original stored chunk one."],
    )
    second_id, _ = _add_paper_with_chunks(
        factory,
        "No Evidence Second",
        ["Original stored chunk two."],
    )
    with factory() as db:
        before = [
            (chunk.id, chunk.text)
            for chunk in db.query(PaperChunk)
            .order_by(PaperChunk.id.asc())
            .all()
        ]
    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        lambda **_kwargs: [],
    )
    llm_factory = Mock(
        side_effect=AssertionError("LLM must not be called without evidence")
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        llm_factory,
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "no evidence",
            "include_future_work": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REVIEW_INSUFFICIENT_EVIDENCE"
    llm_factory.assert_not_called()
    with factory() as db:
        after = [
            (chunk.id, chunk.text)
            for chunk in db.query(PaperChunk)
            .order_by(PaperChunk.id.asc())
            .all()
        ]
    assert after == before


def test_future_work_distinguishes_explicit_and_inferred(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_id, first_chunks = _add_paper_with_chunks(
        factory,
        "Future Work First",
        [
            "Current method reduces routing latency.",
            "Future work will study multi-agent scheduling.",
        ],
    )
    second_id, second_chunks = _add_paper_with_chunks(
        factory,
        "Future Work Second",
        [
            "The approach has a scalability limitation.",
            "A limitation is the small evaluation scale.",
        ],
    )

    def search(**kwargs):
        if kwargs["paper_id"] == first_id:
            return [
                _search_result(
                    first_id,
                    first_chunks[0],
                    1,
                    "Current method reduces routing latency.",
                )
            ]
        return [
            _search_result(
                second_id,
                second_chunks[0],
                1,
                "The approach has a scalability limitation.",
            )
        ]

    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        search,
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: ReviewLLM(
            {
                "title": "Future Work Review",
                "sections": [
                    {
                        "heading": "研究现状",
                        "content": "两篇论文讨论了网络方法。",
                        "citation_ids": [1, 3],
                    }
                ],
                "future_work": [
                    {
                        "kind": "explicit",
                        "summary": "作者计划研究多智能体调度。",
                        "citation_ids": [2],
                    },
                    {
                        "kind": "inferred",
                        "summary": "可扩大评估规模以验证可扩展性。",
                        "citation_ids": [4],
                    },
                ],
            }
        ),
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "future work",
            "include_future_work": True,
        },
    )
    assert response.status_code == 200, response.text
    future_work = response.json()["data"]["future_work"]
    assert {item["kind"] for item in future_work} == {"explicit", "inferred"}
    explicit = next(item for item in future_work if item["kind"] == "explicit")
    inferred = next(item for item in future_work if item["kind"] == "inferred")
    assert "Future work" in explicit["original_evidence"]
    assert "limitation" in inferred["original_evidence"].casefold()


def test_review_timeout_is_identifiable(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_id, first_chunks = _add_paper_with_chunks(
        factory,
        "Timeout First",
        ["Timeout evidence first."],
    )
    second_id, second_chunks = _add_paper_with_chunks(
        factory,
        "Timeout Second",
        ["Timeout evidence second."],
    )
    monkeypatch.setattr(
        literature_review_service,
        "search_chunks_hybrid",
        lambda **kwargs: [
            _search_result(
                kwargs["paper_id"],
                first_chunks[0]
                if kwargs["paper_id"] == first_id
                else second_chunks[0],
                1,
                "Usable evidence.",
            )
        ],
    )
    monkeypatch.setattr(
        literature_review_service,
        "get_llm_client",
        lambda: ReviewLLM(fail=True),
    )

    response = isolated_environment["client"].post(
        "/api/reviews",
        json={
            "paper_ids": [first_id, second_id],
            "topic": "timeout",
            "include_future_work": False,
        },
    )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "REVIEW_TIMEOUT"
