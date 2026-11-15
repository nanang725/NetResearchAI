from __future__ import annotations

import io

import fitz

from app.models.paper import PaperChunk, PaperPage
from app.services import embedding_service, retrieval_service
from app.services.paper_service import create_or_resume_paper


def _four_page_pdf() -> bytes:
    document = fitz.open()
    for page_number in range(1, 5):
        page = document.new_page()
        body = " ".join(
            [
                f"PAGE{page_number}",
                *[
                    f"page-{page_number}-evidence-{index} reliable network routing"
                    for index in range(45)
                ],
            ]
        )
        page.insert_textbox(
            fitz.Rect(50, 50, 550, 790),
            body,
            fontsize=10,
        )
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _create(factory, content: bytes, **kwargs):
    with factory() as db:
        return create_or_resume_paper(
            db=db,
            filename="chunk-order.pdf",
            content=content,
            content_type="application/pdf",
            **kwargs,
        ).id


def _mark_page_failed(factory, paper_id: int, page_number: int = 2) -> None:
    with factory() as db:
        page = (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, page_number=page_number)
            .one()
        )
        page.checkpoint_status = "failed"
        page.checkpoint_error = "simulated retry"
        db.commit()


def _ordered_chunks(factory, paper_id: int):
    with factory() as db:
        return (
            db.query(PaperChunk)
            .filter_by(paper_id=paper_id)
            .order_by(
                PaperChunk.page_number.asc(),
                PaperChunk.page_chunk_index.asc(),
                PaperChunk.chunk_index.asc(),
                PaperChunk.id.asc(),
            )
            .all()
        )


class FakeCollection:
    def __init__(self):
        self.records = {}
        self.upsert_order = []

    def get(self, ids=None, where=None, include=None, **kwargs):
        include = include or []
        selected = [
            vector_id
            for vector_id, record in self.records.items()
            if (
                (ids is not None and vector_id in ids)
                or (
                    ids is None
                    and (
                        where is None
                        or all(
                            record["metadata"].get(key) == value
                            for key, value in where.items()
                        )
                    )
                )
            )
        ]
        result = {"ids": selected}
        if "documents" in include:
            result["documents"] = [
                self.records[vector_id]["document"] for vector_id in selected
            ]
        if "metadatas" in include:
            result["metadatas"] = [
                dict(self.records[vector_id]["metadata"]) for vector_id in selected
            ]
        if "embeddings" in include:
            result["embeddings"] = [
                self.records[vector_id]["embedding"] for vector_id in selected
            ]
        return result

    def upsert(self, ids, embeddings, documents, metadatas):
        for vector_id, embedding, document, metadata in zip(
            ids,
            embeddings,
            documents,
            metadatas,
        ):
            self.records[vector_id] = {
                "embedding": list(embedding),
                "document": document,
                "metadata": dict(metadata),
            }
            self.upsert_order.append(
                (metadata["page_number"], metadata["page_chunk_index"])
            )

    def delete(self, ids=None, where=None):
        if ids is not None:
            for vector_id in ids:
                self.records.pop(vector_id, None)
        elif where is not None:
            for vector_id, record in list(self.records.items()):
                if all(
                    record["metadata"].get(key) == value
                    for key, value in where.items()
                ):
                    self.records.pop(vector_id, None)

    def count(self):
        return len(self.records)


def test_noncontiguous_page_retry_restores_global_chunk_order(
    isolated_environment,
):
    content = _four_page_pdf()
    factory = isolated_environment["session_factory"]
    paper_id = _create(factory, content)
    _mark_page_failed(factory, paper_id)

    parsed_pages = []
    _create(factory, content, page_observer=parsed_pages.append)

    assert parsed_pages == [2]
    chunks = _ordered_chunks(factory, paper_id)
    assert [chunk.page_number for chunk in chunks] == sorted(
        chunk.page_number for chunk in chunks
    )
    for page_number in range(1, 5):
        page_chunks = [chunk for chunk in chunks if chunk.page_number == page_number]
        assert len(page_chunks) > 1
        assert [chunk.page_chunk_index for chunk in page_chunks] == list(
            range(len(page_chunks))
        )
        assert all(f"PAGE{page_number}" in page_chunks[0].text for _ in [0])


def test_page_internal_order_is_stable_across_repeated_retries(
    isolated_environment,
):
    content = _four_page_pdf()
    factory = isolated_environment["session_factory"]
    paper_id = _create(factory, content)

    snapshots = []
    for _ in range(3):
        _mark_page_failed(factory, paper_id)
        parsed_pages = []
        _create(factory, content, page_observer=parsed_pages.append)
        assert parsed_pages == [2]
        chunks = _ordered_chunks(factory, paper_id)
        snapshots.append(
            [
                (
                    chunk.page_number,
                    chunk.page_chunk_index,
                    chunk.text,
                )
                for chunk in chunks
            ]
        )
        with factory() as db:
            page_two_count = (
                db.query(PaperChunk)
                .filter_by(paper_id=paper_id, page_number=2)
                .count()
            )
            assert page_two_count > 1

    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_retrieval_iteration_uses_page_order(
    isolated_environment,
    monkeypatch,
):
    content = _four_page_pdf()
    factory = isolated_environment["session_factory"]
    paper_id = _create(factory, content)
    _mark_page_failed(factory, paper_id)
    _create(factory, content)

    observed_pages = []
    original_is_reference = retrieval_service._is_reference_chunk

    def observe(text):
        match = next(
            (
                page_number
                for page_number in range(1, 5)
                if f"PAGE{page_number}" in text
            ),
            None,
        )
        if match is not None:
            observed_pages.append(match)
        return original_is_reference(text)

    monkeypatch.setattr(retrieval_service, "_is_reference_chunk", observe)
    with factory() as db:
        retrieval_service.detect_reference_chunk_ids(db, paper_id)

    assert observed_pages == sorted(observed_pages)


def test_index_repair_preserves_order_and_stable_vector_mapping(
    isolated_environment,
    monkeypatch,
):
    content = _four_page_pdf()
    factory = isolated_environment["session_factory"]
    paper_id = _create(factory, content)
    _mark_page_failed(factory, paper_id)
    _create(factory, content)

    collection = FakeCollection()
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        embedding_service,
        "encode_texts",
        lambda texts: [[1.0, float(index)] for index, _ in enumerate(texts)],
    )

    with factory() as db:
        embedding_service.index_paper(db, paper_id)

    chunks = _ordered_chunks(factory, paper_id)
    assert collection.upsert_order == [
        (chunk.page_number, chunk.page_chunk_index)
        for chunk in chunks
    ]
    assert set(collection.records) == {
        f"chunk_{chunk.id}" for chunk in chunks
    }
    for chunk in chunks:
        metadata = collection.records[f"chunk_{chunk.id}"]["metadata"]
        assert metadata["page_number"] == chunk.page_number
        assert metadata["page_chunk_index"] == chunk.page_chunk_index
        assert metadata["chunk_id"] == chunk.id
        assert metadata["index_schema_version"] == embedding_service.INDEX_SCHEMA_VERSION

    _mark_page_failed(factory, paper_id)
    _create(factory, content)
    with factory() as db:
        embedding_service.index_paper(db, paper_id)

    current_chunks = _ordered_chunks(factory, paper_id)
    assert set(collection.records) == {
        f"chunk_{chunk.id}" for chunk in current_chunks
    }
    assert [
        (
            collection.records[f"chunk_{chunk.id}"]["metadata"]["page_number"],
            collection.records[f"chunk_{chunk.id}"]["metadata"]["page_chunk_index"],
        )
        for chunk in current_chunks
    ] == [
        (chunk.page_number, chunk.page_chunk_index)
        for chunk in current_chunks
    ]
