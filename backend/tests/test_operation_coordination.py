from __future__ import annotations

import io
import threading
import time

import fitz

from app.models.paper import Paper, PaperChunk
from app.services import embedding_service
from app.services.paper_operation_coordinator import (
    PaperOperationCoordinator,
    paper_operation_coordinator,
)
from app.services.paper_service import delete_paper


def _pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Coordinated network operations and reliable packet delivery.",
        fontsize=11,
    )
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _upload(client, filename: str = "coordination.pdf") -> int:
    response = client.post(
        "/api/papers/upload",
        files={"file": (filename, _pdf(), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


def _wait_for_deletion_pending(paper_id: int, timeout: float = 5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with paper_operation_coordinator._condition:
            state = paper_operation_coordinator._states.get(paper_id)
            if state is not None and state.deletion_pending:
                return True
        time.sleep(0.005)
    return False


def test_delete_waits_for_active_parse_then_removes_paper(
    isolated_environment,
):
    paper_id = _upload(isolated_environment["client"])
    delete_started = threading.Event()
    delete_finished = threading.Event()
    errors = []

    def delete_worker():
        delete_started.set()
        try:
            with isolated_environment["session_factory"]() as db:
                delete_paper(db, paper_id)
        except Exception as exc:
            errors.append(exc)
        finally:
            delete_finished.set()

    with paper_operation_coordinator.parsing(paper_id):
        thread = threading.Thread(target=delete_worker)
        thread.start()
        assert delete_started.wait(5)
        assert _wait_for_deletion_pending(paper_id)
        assert not delete_finished.is_set()

    thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert delete_finished.is_set()
    with isolated_environment["session_factory"]() as db:
        assert db.query(Paper).filter_by(id=paper_id).first() is None


def test_new_parse_waits_while_deletion_is_pending(
    isolated_environment,
):
    paper_id = _upload(isolated_environment["client"])
    parse_acquired = threading.Event()
    parse_finished = threading.Event()

    def parse_worker():
        with paper_operation_coordinator.parsing(paper_id):
            parse_acquired.set()
            parse_finished.set()

    with paper_operation_coordinator.deleting(paper_id):
        thread = threading.Thread(target=parse_worker)
        thread.start()
        assert not parse_acquired.wait(0.1)

    thread.join(5)
    assert not thread.is_alive()
    assert parse_acquired.is_set()
    assert parse_finished.is_set()


def test_index_repair_waits_until_parsing_finishes(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(isolated_environment["client"])

    class FakeCollection:
        def __init__(self):
            self.records = {}

        def get(self, **kwargs):
            include = kwargs.get("include", [])
            return {
                "ids": [],
                **({"documents": []} if "documents" in include else {}),
                **({"metadatas": []} if "metadatas" in include else {}),
                **({"embeddings": []} if "embeddings" in include else {}),
            }

        def upsert(self, ids, embeddings, documents, metadatas):
            for vector_id in ids:
                self.records[vector_id] = True

        def count(self):
            return len(self.records)

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
    index_started = threading.Event()
    index_finished = threading.Event()
    results = []
    errors = []

    def index_worker():
        index_started.set()
        try:
            with isolated_environment["session_factory"]() as db:
                results.append(embedding_service.index_paper(db, paper_id))
        except Exception as exc:
            errors.append(exc)
        finally:
            index_finished.set()

    with paper_operation_coordinator.parsing(paper_id):
        thread = threading.Thread(target=index_worker)
        thread.start()
        assert index_started.wait(5)
        assert not index_finished.wait(0.1)
        assert collection.count() == 0

    thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert results[0]["status"] == "indexed"
    assert collection.count() > 0


def test_parsing_exception_releases_coordination_state():
    coordinator = PaperOperationCoordinator()
    paper_id = 123
    try:
        with coordinator.parsing(paper_id):
            raise RuntimeError("simulated parse failure")
    except RuntimeError:
        pass

    with coordinator._condition:
        assert paper_id not in coordinator._states
    with coordinator.parsing(paper_id):
        pass


def test_new_process_coordinator_does_not_inherit_old_parsing_state():
    old_coordinator = PaperOperationCoordinator()
    paper_id = 456
    with old_coordinator.parsing(paper_id):
        assert paper_id in old_coordinator._states

    restarted_coordinator = PaperOperationCoordinator()
    assert restarted_coordinator._states == {}
    with restarted_coordinator.parsing(paper_id):
        assert paper_id in restarted_coordinator._states


def test_orphan_cleanup_cannot_remove_vector_committed_after_parse_recheck(
    isolated_environment,
):
    paper_id = _upload(isolated_environment["client"], "race-cleanup.pdf")
    old_vector_id = "chunk_999999"

    class BlockingCollection:
        def __init__(self):
            self.records = {
                old_vector_id: {
                    "document": "old orphan",
                    "metadata": {"paper_id": 999999, "chunk_id": 999999},
                }
            }
            self.delete_entered = threading.Event()
            self.release_delete = threading.Event()

        def get(self, ids=None, include=None, limit=None, offset=0, **kwargs):
            include = include or []
            if ids is not None:
                selected = [item for item in ids if item in self.records]
            else:
                selected = list(self.records)[offset:offset + (limit or len(self.records))]
            return {
                "ids": selected,
                **(
                    {"metadatas": [self.records[item]["metadata"] for item in selected]}
                    if "metadatas" in include
                    else {}
                ),
            }

        def delete(self, ids=None, where=None):
            self.delete_entered.set()
            assert self.release_delete.wait(5)
            for vector_id in ids or []:
                self.records.pop(vector_id, None)

        def count(self):
            return len(self.records)

    collection = BlockingCollection()
    cleanup_finished = threading.Event()
    commit_finished = threading.Event()
    errors = []

    def cleanup_worker():
        try:
            with isolated_environment["session_factory"]() as db:
                embedding_service._delete_orphaned_index_vectors(
                    db,
                    collection,
                    batch_size=10,
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            cleanup_finished.set()

    def parse_commit_worker():
        try:
            with paper_operation_coordinator.parsing(paper_id):
                with paper_operation_coordinator.collection_mutation():
                    with isolated_environment["session_factory"]() as db:
                        db.add(
                            PaperChunk(
                                id=999999,
                                paper_id=paper_id,
                                page_number=1,
                                chunk_index=999999,
                                text="new coordinated chunk",
                                content_type="paragraph",
                            )
                        )
                        db.commit()
                    collection.records[old_vector_id] = {
                        "document": "new coordinated chunk",
                        "metadata": {"paper_id": paper_id, "chunk_id": 999999},
                    }
        except Exception as exc:
            errors.append(exc)
        finally:
            commit_finished.set()

    cleanup_thread = threading.Thread(target=cleanup_worker)
    cleanup_thread.start()
    assert collection.delete_entered.wait(5)
    commit_thread = threading.Thread(target=parse_commit_worker)
    commit_thread.start()
    assert not commit_finished.wait(0.1)

    collection.release_delete.set()
    cleanup_thread.join(5)
    commit_thread.join(5)

    assert not errors
    assert cleanup_finished.is_set()
    assert commit_finished.is_set()
    assert old_vector_id in collection.records
    assert collection.records[old_vector_id]["document"] == "new coordinated chunk"
