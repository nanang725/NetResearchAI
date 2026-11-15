from __future__ import annotations

import io
import threading
import time

import fitz
import pytest

from app.models.paper import Paper, PaperChunk
from app.services import embedding_service
from app.services.paper_operation_coordinator import paper_operation_coordinator
from app.services.paper_service import (
    delete_paper,
    finish_paper_deletion,
)


class FakeCollection:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.upsert_hook = None

    def get(self, where=None, include=None, **_kwargs):
        include = include or []
        with self.lock:
            records = [
                (vector_id, record)
                for vector_id, record in self.records.items()
                if where is None
                or all(record["metadatas"].get(key) == value for key, value in where.items())
            ]
            result = {"ids": [vector_id for vector_id, _ in records]}
            if "documents" in include:
                result["documents"] = [record["documents"] for _, record in records]
            if "metadatas" in include:
                result["metadatas"] = [record["metadatas"] for _, record in records]
            if "embeddings" in include:
                result["embeddings"] = [record["embeddings"] for _, record in records]
            return result

    def upsert(self, ids, embeddings, documents, metadatas):
        hook = self.upsert_hook
        if hook is not None:
            hook()
        with self.lock:
            for vector_id, embedding, document, metadata in zip(
                ids,
                embeddings,
                documents,
                metadatas,
            ):
                self.records[vector_id] = {
                    "embeddings": list(embedding),
                    "documents": document,
                    "metadatas": metadata,
                }

    def delete(self, ids=None, where=None):
        with self.lock:
            if ids is not None:
                for vector_id in ids:
                    self.records.pop(vector_id, None)
            elif where is not None:
                for vector_id, record in list(self.records.items()):
                    if all(
                        record["metadatas"].get(key) == value
                        for key, value in where.items()
                    ):
                        self.records.pop(vector_id, None)

    def count(self):
        with self.lock:
            return len(self.records)


def _upload_paper(environment, filename: str) -> tuple[int, str]:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), f"Research paper {filename} on network systems.")
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    response = environment["client"].post(
        "/api/papers/upload",
        files={"file": (filename, buffer.getvalue(), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    paper = response.json()["data"]
    return paper["id"], paper["stored_filename"]


def _configure_indexer(monkeypatch, collection: FakeCollection) -> None:
    monkeypatch.setattr(embedding_service, "get_chroma_collection", lambda: collection)
    monkeypatch.setattr(
        embedding_service,
        "encode_texts",
        lambda texts: [[float(index + 1), 1.0] for index, _ in enumerate(texts)],
    )


def _index(session_factory, paper_id: int):
    with session_factory() as db:
        return embedding_service.index_paper(db, paper_id)


def _wait_for_deletion_request(paper_id: int, timeout: float = 5) -> bool:
    coordinator = paper_operation_coordinator
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with coordinator._condition:
            state = coordinator._states.get(paper_id)
            if state is not None and state.deletion_pending:
                return True
        time.sleep(0.005)
    return False


def _wait_for_users(paper_id: int, expected: int, timeout: float = 5) -> bool:
    coordinator = paper_operation_coordinator
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with coordinator._condition:
            state = coordinator._states.get(paper_id)
            if state is not None and state.users >= expected:
                return True
        time.sleep(0.005)
    return False


def test_index_then_delete_removes_paper_and_all_vectors(
    isolated_environment,
    monkeypatch,
):
    paper_id, stored_filename = _upload_paper(isolated_environment, "normal.pdf")
    collection = FakeCollection()
    _configure_indexer(monkeypatch, collection)

    indexed = _index(isolated_environment["session_factory"], paper_id)
    assert indexed["status"] == "indexed"
    assert collection.count() > 0

    with isolated_environment["session_factory"]() as db:
        deletion = delete_paper(db, paper_id)
    pending = finish_paper_deletion(
        deletion,
        embedding_service.delete_paper_vectors,
        isolated_environment["upload_dir"],
    )

    assert pending is False
    assert collection.count() == 0
    with isolated_environment["session_factory"]() as db:
        assert db.query(Paper).filter_by(id=paper_id).first() is None
    assert not (isolated_environment["upload_dir"] / stored_filename).exists()


def test_index_skips_unchanged_vectors_and_replaces_changed_chunk(
    isolated_environment,
    monkeypatch,
):
    paper_id, _ = _upload_paper(isolated_environment, "incremental.pdf")
    collection = FakeCollection()
    _configure_indexer(monkeypatch, collection)
    encoded_batches = []

    def encode(texts):
        encoded_batches.append(list(texts))
        return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]

    monkeypatch.setattr(embedding_service, "encode_texts", encode)
    with isolated_environment["session_factory"]() as db:
        first = embedding_service.index_paper(db, paper_id)
        unchanged = embedding_service.index_paper(db, paper_id)
        chunk = db.query(PaperChunk).filter_by(paper_id=paper_id).one()
        chunk.text = "Updated network research content."
        db.commit()
        changed = embedding_service.index_paper(db, paper_id)

    assert first["status"] == unchanged["status"] == changed["status"] == "indexed"
    assert len(encoded_batches) == 2
    records = collection.get(where={"paper_id": paper_id}, include=["metadatas"])
    assert len(records["ids"]) == 1
    assert records["metadatas"][0]["content_type"] == "paragraph"


def test_delete_waits_for_active_index_and_blocks_new_index_until_deleted(
    isolated_environment,
    monkeypatch,
):
    paper_id, _ = _upload_paper(isolated_environment, "racing.pdf")
    collection = FakeCollection()
    _configure_indexer(monkeypatch, collection)
    upsert_entered = threading.Event()
    release_upsert = threading.Event()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    new_index_started = threading.Event()
    new_index_finished = threading.Event()
    index_errors: list[Exception] = []
    new_index_errors: list[Exception] = []

    def block_first_upsert():
        collection.upsert_hook = None
        upsert_entered.set()
        if not release_upsert.wait(5):
            raise TimeoutError("test did not release the active index")

    collection.upsert_hook = block_first_upsert

    def delete_worker():
        delete_started.set()
        with isolated_environment["session_factory"]() as db:
            deletion = delete_paper(db, paper_id)
        finish_paper_deletion(
            deletion,
            embedding_service.delete_paper_vectors,
            isolated_environment["upload_dir"],
        )
        delete_finished.set()

    def new_index_worker():
        new_index_started.set()
        try:
            _index(isolated_environment["session_factory"], paper_id)
        except Exception as exc:
            new_index_errors.append(exc)
        finally:
            new_index_finished.set()

    def active_index_worker():
        try:
            _index(isolated_environment["session_factory"], paper_id)
        except Exception as exc:
            index_errors.append(exc)

    index_thread = threading.Thread(target=active_index_worker)
    delete_thread = threading.Thread(target=delete_worker)
    new_index_thread = threading.Thread(target=new_index_worker)
    index_thread.start()
    try:
        assert upsert_entered.wait(5)
        delete_thread.start()
        assert delete_started.wait(5)
        assert _wait_for_deletion_request(paper_id)
        assert not delete_finished.is_set()

        new_index_thread.start()
        assert new_index_started.wait(5)
        assert _wait_for_users(paper_id, expected=3)
        assert not new_index_finished.wait(0.05)

        release_upsert.set()
        index_thread.join(5)
        delete_thread.join(5)
        new_index_thread.join(5)

        assert not index_thread.is_alive()
        assert not delete_thread.is_alive()
        assert not new_index_thread.is_alive()
        assert not index_errors
        assert delete_finished.is_set()
        assert len(new_index_errors) == 1
        assert isinstance(new_index_errors[0], ValueError)
        assert collection.count() == 0
        with isolated_environment["session_factory"]() as db:
            assert db.query(Paper).filter_by(id=paper_id).first() is None
    finally:
        release_upsert.set()
        for thread in (index_thread, delete_thread, new_index_thread):
            if thread.ident is not None:
                thread.join(5)


def test_delete_of_one_paper_does_not_block_another_papers_index(
    isolated_environment,
    monkeypatch,
):
    deleting_paper_id, _ = _upload_paper(isolated_environment, "deleting.pdf")
    indexing_paper_id, _ = _upload_paper(isolated_environment, "independent.pdf")
    collection = FakeCollection()
    _configure_indexer(monkeypatch, collection)
    index_done = threading.Event()
    index_errors: list[Exception] = []

    def index_other_paper():
        try:
            _index(isolated_environment["session_factory"], indexing_paper_id)
        except Exception as exc:
            index_errors.append(exc)
        finally:
            index_done.set()

    with paper_operation_coordinator.deleting(deleting_paper_id):
        thread = threading.Thread(target=index_other_paper)
        thread.start()
        assert index_done.wait(5)
        thread.join(5)

    assert not index_errors
    assert collection.get(where={"paper_id": indexing_paper_id}, include=[])["ids"]
    assert not collection.get(where={"paper_id": deleting_paper_id}, include=[])["ids"]


def test_index_failure_restores_previous_vectors(
    isolated_environment,
    monkeypatch,
):
    paper_id, _ = _upload_paper(isolated_environment, "index-failure.pdf")
    collection = FakeCollection()
    collection.records["chunk_old"] = {
        "embeddings": [0.25, 0.75],
        "documents": "previous indexed content",
        "metadatas": {"paper_id": paper_id, "chunk_id": 999, "page_number": 1},
    }
    _configure_indexer(monkeypatch, collection)
    remaining_failures = {"count": 1}
    original_upsert = collection.upsert

    def fail_once(*args, **kwargs):
        if remaining_failures["count"]:
            remaining_failures["count"] -= 1
            raise RuntimeError("simulated vector write failure")
        original_upsert(*args, **kwargs)

    monkeypatch.setattr(collection, "upsert", fail_once)

    with pytest.raises(RuntimeError, match="simulated vector write failure"):
        _index(isolated_environment["session_factory"], paper_id)

    assert collection.get(where={"paper_id": paper_id}, include=[])["ids"] == ["chunk_old"]
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).filter_by(id=paper_id).one()
        assert paper.index_status == "failed"


def test_failed_vector_cleanup_remains_retryable(
    isolated_environment,
    monkeypatch,
):
    paper_id, _ = _upload_paper(isolated_environment, "delete-failure.pdf")
    collection = FakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    assert collection.count() > 0

    with isolated_environment["session_factory"]() as db:
        deletion = delete_paper(db, paper_id)

    def fail_delete(_paper_id: int):
        raise RuntimeError("simulated vector deletion failure")

    pending = finish_paper_deletion(
        deletion,
        fail_delete,
        isolated_environment["upload_dir"],
    )
    assert pending is True
    assert deletion.journal_path.is_file()
    assert collection.count() > 0

    pending = finish_paper_deletion(
        deletion,
        embedding_service.delete_paper_vectors,
        isolated_environment["upload_dir"],
    )
    assert pending is False
    assert collection.count() == 0
    assert not deletion.journal_path.exists()
