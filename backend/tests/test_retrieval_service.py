import hashlib
import logging
from unittest.mock import Mock

import pytest
from rank_bm25 import BM25Okapi
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.paper import Paper, PaperChunk
from app.services import retrieval_service


@pytest.fixture()
def db(tmp_path):
    database_path = tmp_path / "retrieval-test.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    session = test_session()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_paper(db, title: str, texts: list[str]):
    paper = Paper(
        title=title,
        file_hash=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        original_filename="retrieval-test.pdf",
        stored_filename="retrieval-test.pdf",
        file_path="unused-retrieval-test.pdf",
        file_size=100,
        content_type="application/pdf",
        page_count=len(texts),
        char_count=sum(len(text) for text in texts),
        parse_status="parsed",
        index_status="indexed",
    )
    db.add(paper)
    db.flush()

    chunks = []
    for index, text in enumerate(texts):
        chunk = PaperChunk(
            paper_id=paper.id,
            page_number=index + 1,
            chunk_index=index,
            text=text,
        )
        db.add(chunk)
        chunks.append(chunk)

    db.commit()
    return paper, chunks


def _result(paper: Paper, chunk: PaperChunk, method: str, score: float = 1.0):
    return {
        "chunk_id": chunk.id,
        "paper_id": paper.id,
        "paper_title": paper.title,
        "page_number": chunk.page_number,
        "text": chunk.text,
        "retrieval_method": method,
        "retrieval_score": score,
    }


def test_bm25_returns_single_matching_chunk_with_nonpositive_score(db):
    text = (
        "The quasar protocol improves reliable packet delivery in "
        "large computer networks."
    )
    paper, chunks = _add_paper(db, "Network Study", [text])
    document_tokens = retrieval_service.tokenize(f"{paper.title} {text}")
    query_tokens = retrieval_service.tokenize("quasar")
    bm25_score = float(
        BM25Okapi([document_tokens]).get_scores(query_tokens)[0]
    )

    assert bm25_score <= 0

    results = retrieval_service.search_chunks_bm25(
        db,
        "quasar",
        paper_id=paper.id,
    )

    assert [item["chunk_id"] for item in results] == [chunks[0].id]
    assert results[0]["retrieval_score"] == round(bm25_score, 6)


def test_bm25_keeps_chinese_tokenizer_hits_with_nonpositive_score(db):
    text = (
        "本文研究网络拥塞控制方法，并分析不同网络协议在数据包传输中的"
        "可靠性与性能。"
    )
    paper, chunks = _add_paper(db, "网络研究", [text])
    document_tokens = retrieval_service.tokenize(f"{paper.title} {text}")
    query_tokens = retrieval_service.tokenize("拥塞控制")
    bm25_score = float(
        BM25Okapi([document_tokens]).get_scores(query_tokens)[0]
    )

    assert bm25_score <= 0
    results = retrieval_service.search_chunks_bm25(
        db,
        "拥塞控制",
        paper_id=paper.id,
    )

    assert [item["chunk_id"] for item in results] == [chunks[0].id]
    assert results[0]["retrieval_score"] == round(bm25_score, 6)


def test_bm25_does_not_match_query_present_only_in_paper_title(db):
    text = (
        "This protocol improves reliable packet delivery in "
        "large computer networks."
    )
    paper = _add_paper(db, "Quasar Network Study", [text])[0]

    results = retrieval_service.search_chunks_bm25(
        db,
        "quasar",
        paper_id=paper.id,
    )

    assert results == []


def test_single_paper_keeps_bm25_results_when_vector_search_fails(
    db,
    monkeypatch,
    caplog,
):
    text = (
        "The quasar protocol improves reliable packet delivery in "
        "large computer networks."
    )
    paper, chunks = _add_paper(db, "Network Study", [text])

    monkeypatch.setattr(
        retrieval_service,
        "search_similar_chunks",
        Mock(side_effect=RuntimeError("simulated vector failure")),
    )
    with caplog.at_level(logging.WARNING):
        results = retrieval_service.search_chunks_hybrid(
            db,
            "quasar",
            paper_id=paper.id,
        )

    assert [item["chunk_id"] for item in results] == [chunks[0].id]
    assert results[0]["retrieval_method"] == "bm25"
    assert results[0]["retrieval_sources"] == ["bm25"]
    assert "Vector retrieval failed" in caplog.text
    assert "available channels=BM25" in caplog.text


def test_all_papers_keeps_bm25_results_when_vector_search_fails(
    db,
    monkeypatch,
):
    text = (
        "The quasar protocol improves reliable packet delivery in "
        "large computer networks."
    )
    paper, chunks = _add_paper(db, "Network Study", [text])

    monkeypatch.setattr(
        retrieval_service,
        "search_similar_chunks",
        Mock(side_effect=RuntimeError("simulated vector failure")),
    )

    results = retrieval_service.search_chunks_hybrid(db, "quasar")

    assert [item["chunk_id"] for item in results] == [chunks[0].id]
    assert results[0]["paper_id"] == paper.id
    assert results[0]["retrieval_method"] == "bm25"


def test_vector_results_survive_bm25_failure(db, monkeypatch, caplog):
    text = (
        "The quasar protocol improves reliable packet delivery in "
        "large computer networks."
    )
    paper, chunks = _add_paper(db, "Network Study", [text])
    vector_result = _result(paper, chunks[0], "vector")

    monkeypatch.setattr(
        retrieval_service,
        "search_chunks_bm25",
        Mock(side_effect=RuntimeError("simulated BM25 failure")),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_similar_chunks",
        Mock(return_value=[vector_result]),
    )
    with caplog.at_level(logging.WARNING):
        results = retrieval_service.search_chunks_hybrid(
            db,
            "quasar",
            paper_id=paper.id,
        )

    assert [item["chunk_id"] for item in results] == [chunks[0].id]
    assert results[0]["retrieval_method"] == "vector"
    assert results[0]["retrieval_sources"] == ["vector"]
    assert "BM25 retrieval failed" in caplog.text
    assert "available channels=vector" in caplog.text


def test_both_successful_channels_are_fused_with_rrf(db, monkeypatch):
    texts = [
        "The quasar protocol improves reliable packet delivery in networks.",
        "The nebula protocol improves congestion control in computer networks.",
        "The pulsar protocol improves scheduling of packets in large networks.",
    ]
    paper, chunks = _add_paper(db, "Network Study", texts)
    bm25_results = [
        _result(paper, chunks[0], "bm25"),
        _result(paper, chunks[1], "bm25"),
    ]
    vector_results = [
        _result(paper, chunks[0], "vector"),
        _result(paper, chunks[2], "vector"),
    ]
    monkeypatch.setattr(
        retrieval_service,
        "search_chunks_bm25",
        Mock(return_value=bm25_results),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_similar_chunks",
        Mock(return_value=vector_results),
    )

    results = retrieval_service.search_chunks_hybrid(
        db,
        "quasar",
        paper_id=paper.id,
    )

    assert [item["chunk_id"] for item in results] == [
        chunks[0].id,
        chunks[1].id,
        chunks[2].id,
    ]
    assert results[0]["retrieval_method"] == "hybrid"
    assert results[0]["retrieval_sources"] == ["bm25", "vector"]
    assert results[0]["retrieval_score"] == round(2 / 61, 8)
    assert set(results[0]) == {
        "chunk_id",
        "paper_id",
        "paper_title",
        "page_number",
        "text",
        "retrieval_method",
        "retrieval_score",
        "retrieval_sources",
        "bm25_rank",
        "vector_rank",
        "bm25_score",
        "vector_score",
    }
    assert results[1]["retrieval_method"] == "bm25"
    assert results[2]["retrieval_method"] == "vector"


def test_reference_chunks_are_filtered_from_both_channels(db, monkeypatch):
    body_text = (
        "The quasar protocol improves reliable packet delivery in "
        "large computer networks."
    )
    reference_text = (
        "References\n"
        "[1] Doe, A., et al. Quasar Networks, 2022, Journal of Networked "
        "Systems, Vol. 12, pp. 1-12."
    )
    paper, chunks = _add_paper(
        db,
        "Network Study",
        [body_text, reference_text],
    )
    assert retrieval_service.detect_reference_chunk_ids(db, paper.id) == {
        chunks[1].id
    }
    monkeypatch.setattr(
        retrieval_service,
        "search_similar_chunks",
        Mock(return_value=[
            _result(paper, chunks[0], "vector"),
            _result(paper, chunks[1], "vector"),
        ]),
    )

    results = retrieval_service.search_chunks_hybrid(
        db,
        "quasar",
        paper_id=paper.id,
    )

    assert [item["chunk_id"] for item in results] == [chunks[0].id]
    assert results[0]["retrieval_method"] == "hybrid"


def test_returns_empty_results_and_logs_when_both_channels_fail(
    db,
    monkeypatch,
    caplog,
):
    paper = _add_paper(
        db,
        "Network Study",
        [
            "The quasar protocol improves reliable packet delivery in "
            "large computer networks."
        ],
    )[0]

    monkeypatch.setattr(
        retrieval_service,
        "search_chunks_bm25",
        Mock(side_effect=RuntimeError("simulated BM25 failure")),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_similar_chunks",
        Mock(side_effect=RuntimeError("simulated vector failure")),
    )
    with caplog.at_level(logging.WARNING):
        results = retrieval_service.search_chunks_hybrid(
            db,
            "quasar",
            paper_id=paper.id,
        )

    assert results == []
    assert "BM25 retrieval failed" in caplog.text
    assert "Vector retrieval failed" in caplog.text
    assert "available channels=none" in caplog.text
