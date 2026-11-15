from __future__ import annotations

import hashlib
import io
import json
import threading

import fitz
import pytest

from app.models.paper import Paper, PaperChunk
from app.services import embedding_service
from app.services.ingestion_service import create_index_rebuild_job
from app.services.paper_operation_coordinator import paper_operation_coordinator


class ContentAwareFakeCollection:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.upsert_hook = None
        self.fail_next_get_ids = False
        self.delete_noop_ids: set[str] = set()
        self.delete_only_ids: set[str] | None = None
        self.delete_hook = None

    def get(
        self,
        ids=None,
        where=None,
        include=None,
        limit=None,
        offset=0,
    ):
        include = include or []
        if ids is not None and self.fail_next_get_ids:
            self.fail_next_get_ids = False
            raise RuntimeError("simulated partial vector read failure")
        with self.lock:
            if ids is not None:
                selected = [
                    vector_id
                    for vector_id in ids
                    if vector_id in self.records
                ]
            else:
                selected = [
                    vector_id
                    for vector_id, record in self.records.items()
                    if where is None
                    or all(
                        record["metadata"].get(key) == value
                        for key, value in where.items()
                    )
                ]
                selected = selected[offset:] if limit is None else selected[offset:offset + limit]
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
                    list(self.records[vector_id]["embedding"]) for vector_id in selected
                ]
            return result

    def upsert(self, ids, embeddings, documents, metadatas):
        hook = self.upsert_hook
        if hook is not None:
            hook(ids, embeddings, documents, metadatas)
        with self.lock:
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

    def delete(self, ids=None, where=None):
        hook = self.delete_hook
        if hook is not None:
            hook(ids, where)
        with self.lock:
            if ids is not None:
                for vector_id in ids:
                    if vector_id in self.delete_noop_ids:
                        continue
                    if (
                        self.delete_only_ids is not None
                        and vector_id not in self.delete_only_ids
                    ):
                        continue
                    self.records.pop(vector_id, None)
            elif where is not None:
                for vector_id, record in list(self.records.items()):
                    if all(
                        record["metadata"].get(key) == value
                        for key, value in where.items()
                    ):
                        self.records.pop(vector_id, None)

    def count(self):
        with self.lock:
            return len(self.records)


def _pdf(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, 520, 700),
        text,
        fontsize=11,
    )
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _upload(client, filename: str, text: str) -> int:
    response = client.post(
        "/api/papers/upload",
        files={"file": (filename, _pdf(text), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


def _configure_indexer(monkeypatch, collection: ContentAwareFakeCollection) -> None:
    monkeypatch.setattr(embedding_service, "get_chroma_collection", lambda: collection)
    monkeypatch.setattr(
        embedding_service,
        "encode_texts",
        lambda texts: [[float(index + 1), 1.0] for index, _ in enumerate(texts)],
    )


def _index(session_factory, paper_id: int):
    with session_factory() as db:
        return embedding_service.index_paper(db, paper_id)


def _add_chunk(session_factory, paper_id: int, text: str) -> int:
    with session_factory() as db:
        last_index = (
            db.query(PaperChunk.chunk_index)
            .filter(PaperChunk.paper_id == paper_id)
            .order_by(PaperChunk.chunk_index.desc())
            .scalar()
        )
        last_page_index = (
            db.query(PaperChunk.page_chunk_index)
            .filter(
                PaperChunk.paper_id == paper_id,
                PaperChunk.page_number == 1,
            )
            .order_by(PaperChunk.page_chunk_index.desc())
            .scalar()
        )
        chunk = PaperChunk(
            paper_id=paper_id,
            page_number=1,
            page_chunk_index=(
                last_page_index if last_page_index is not None else -1
            ) + 1,
            chunk_index=(last_index if last_index is not None else -1) + 1,
            text=text,
            content_type="paragraph",
        )
        db.add(chunk)
        db.commit()
        return int(chunk.id)


def _audit(environment, collection, paper_id: int | None = None):
    with environment["session_factory"]() as db:
        if paper_id is None:
            return embedding_service.audit_index_consistency(db, batch_size=2)
        return embedding_service.audit_paper_index_consistency(
            db,
            paper_id,
            batch_size=2,
        )


def test_content_hash_is_stable_and_tracks_content_and_key_metadata(
    isolated_environment,
):
    paper_id = _upload(
        isolated_environment["client"],
        "digest.pdf",
        "Reliable routing in computer networks.",
    )
    with isolated_environment["session_factory"]() as db:
        chunk = db.query(PaperChunk).filter_by(paper_id=paper_id).one()
        paper = db.query(Paper).filter_by(id=paper_id).one()

        first = embedding_service.build_chunk_index_metadata(chunk, paper)
        second = embedding_service.build_chunk_index_metadata(chunk, paper)
        assert first == second
        assert first["content_hash"] == second["content_hash"]
        assert first["index_fingerprint"] == second["index_fingerprint"]

        chunk.text = "Reliable routing in computer networks.\nUpdated evidence."
        changed_content = embedding_service.build_chunk_index_metadata(chunk, paper)
        assert changed_content["content_hash"] != first["content_hash"]
        assert changed_content["index_fingerprint"] != first["index_fingerprint"]

        chunk.content_type = "table"
        changed_metadata = embedding_service.build_chunk_index_metadata(chunk, paper)
        assert changed_metadata["index_fingerprint"] != changed_content["index_fingerprint"]

    assert embedding_service.compute_chunk_content_hash("Ａ\r\nB") == (
        embedding_service.compute_chunk_content_hash("A\nB")
    )


def test_normal_index_is_content_aware_consistent(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "consistent.pdf",
        "A network protocol study with reproducible evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)

    assert _index(isolated_environment["session_factory"], paper_id)["status"] == "indexed"
    audit = _audit(isolated_environment, collection)

    assert audit["consistent"] is True
    assert audit["missing_vector_count"] == 0
    assert audit["stale_vector_count"] == 0
    assert audit["orphan_vector_count"] == 0
    assert audit["index_schema_version"] == embedding_service.INDEX_SCHEMA_VERSION


def test_same_id_changed_content_is_stale_and_targeted_index_repairs_it(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "stale-content.pdf",
        "Original network evidence for content-aware indexing.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)

    with isolated_environment["session_factory"]() as db:
        chunk = db.query(PaperChunk).filter_by(paper_id=paper_id).one()
        chunk.text = "Replacement network evidence with changed meaning."
        db.commit()

    stale = _audit(isolated_environment, collection)
    assert stale["consistent"] is False
    assert stale["stale_vector_count"] == 1
    assert stale["affected_paper_ids"] == [paper_id]
    assert any(
        "document_content_changed" in issue["reasons"]
        for issue in stale["issues"]
        if issue["status"] == "stale"
    )

    _index(isolated_environment["session_factory"], paper_id)
    repaired = _audit(isolated_environment, collection)
    assert repaired["consistent"] is True


def test_same_document_with_changed_key_metadata_is_stale(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "stale-metadata.pdf",
        "Metadata-sensitive retrieval evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)

    with isolated_environment["session_factory"]() as db:
        chunk = db.query(PaperChunk).filter_by(paper_id=paper_id).one()
        chunk.page_number = 2
        chunk.content_type = "table"
        db.commit()

    audit = _audit(isolated_environment, collection)
    assert audit["stale_vector_count"] == 1
    reasons = audit["issues"][0]["reasons"]
    assert "metadata_changed:page_number" in reasons
    assert "metadata_changed:content_type" in reasons


def test_missing_vector_is_reported_and_paper_job_is_targeted(
    isolated_environment_no_worker,
    monkeypatch,
):
    isolated_environment = isolated_environment_no_worker
    paper_id = _upload(
        isolated_environment["client"],
        "missing.pdf",
        "Missing vector repair evidence.",
    )
    other_paper_id = _upload(
        isolated_environment["client"],
        "untouched.pdf",
        "Independent vector evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    _index(isolated_environment["session_factory"], other_paper_id)

    with isolated_environment["session_factory"]() as db:
        chunk_id = db.query(PaperChunk.id).filter_by(paper_id=paper_id).scalar()
        vector_id = f"chunk_{chunk_id}"
        job = create_index_rebuild_job(db, paper_ids=[paper_id])
        job_paper_ids = [item.paper_id for item in job.items]
    collection.delete(ids=[vector_id])

    audit = _audit(isolated_environment, collection)
    assert audit["missing_vector_count"] == 1
    assert audit["affected_paper_ids"] == [paper_id]
    assert job_paper_ids == [paper_id]

    _index(isolated_environment["session_factory"], paper_id)
    assert _audit(isolated_environment, collection)["consistent"] is True


def test_confirmed_orphan_is_removed_without_touching_valid_papers(
    isolated_environment,
    monkeypatch,
):
    first_id = _upload(
        isolated_environment["client"],
        "orphan-valid-one.pdf",
        "First valid paper vector.",
    )
    second_id = _upload(
        isolated_environment["client"],
        "orphan-valid-two.pdf",
        "Second valid paper vector.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], first_id)
    _index(isolated_environment["session_factory"], second_id)
    valid_ids = set(collection.records)
    collection.records["chunk_999999"] = {
        "embedding": [9.0, 9.0],
        "document": "orphaned content",
        "metadata": {"paper_id": 999999, "chunk_id": 999999},
    }

    audit = _audit(isolated_environment, collection)
    assert audit["orphan_vector_count"] == 1
    assert audit["affected_paper_ids"] == []

    with isolated_environment["session_factory"]() as db:
        result = embedding_service.reconcile_index_consistency(db)
    assert result["consistent"] is True
    assert set(collection.records) == valid_ids


def test_legacy_metadata_is_stale_and_upgrades_without_duplicate_vectors(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "legacy.pdf",
        "Legacy metadata compatibility evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)

    vector_id, record = next(iter(collection.records.items()))
    record["metadata"] = {
        key: value
        for key, value in record["metadata"].items()
        if key
        not in {
            "content_hash",
            "content_hash_algorithm",
            "content_normalization_version",
            "index_fingerprint",
            "index_schema_version",
        }
    }

    legacy = _audit(isolated_environment, collection)
    assert legacy["consistent"] is False
    assert legacy["stale_vector_count"] == 1
    assert legacy["legacy_vector_count"] == 1
    assert legacy["legacy_vector_ids"] == [vector_id]

    _index(isolated_environment["session_factory"], paper_id)
    upgraded = _audit(isolated_environment, collection)
    assert upgraded["consistent"] is True
    assert collection.count() == 1
    assert "index_fingerprint" in collection.records[vector_id]["metadata"]


def test_partial_vector_write_failure_is_retryable_and_idempotent(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "partial-write.pdf",
        "First retrievable chunk for partial write testing.",
    )
    _add_chunk(
        isolated_environment["session_factory"],
        paper_id,
        "Second retrievable chunk for partial write testing.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)

    def partial_write(ids, embeddings, documents, metadatas):
        collection.upsert_hook = None
        vector_id = ids[0]
        collection.records[vector_id] = {
            "embedding": list(embeddings[0]),
            "document": documents[0],
            "metadata": dict(metadatas[0]),
        }
        raise RuntimeError("simulated partial vector write")

    collection.upsert_hook = partial_write
    with pytest.raises(RuntimeError, match="partial vector write"):
        _index(isolated_environment["session_factory"], paper_id)

    failed_audit = _audit(isolated_environment, collection)
    assert failed_audit["consistent"] is False
    assert failed_audit["missing_vector_count"] == 2

    _index(isolated_environment["session_factory"], paper_id)
    repaired = _audit(isolated_environment, collection)
    assert repaired["consistent"] is True
    with isolated_environment["session_factory"]() as db:
        chunk_count = db.query(PaperChunk).filter_by(paper_id=paper_id).count()
    assert collection.count() == chunk_count


def test_targeted_repair_isolated_from_missing_and_stale_other_papers(
    isolated_environment,
    monkeypatch,
):
    first_id = _upload(
        isolated_environment["client"],
        "isolated-first.pdf",
        "First paper content that will become stale.",
    )
    second_id = _upload(
        isolated_environment["client"],
        "isolated-second.pdf",
        "Second paper content that must remain isolated.",
    )
    _add_chunk(
        isolated_environment["session_factory"],
        second_id,
        "A second chunk for the independent paper.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], first_id)
    _index(isolated_environment["session_factory"], second_id)

    with isolated_environment["session_factory"]() as db:
        first_chunk = db.query(PaperChunk).filter_by(paper_id=first_id).one()
        first_chunk.text = "First paper changed content."
        second_chunks = (
            db.query(PaperChunk)
            .filter_by(paper_id=second_id)
            .order_by(PaperChunk.id.asc())
            .all()
        )
        db.commit()
        missing_vector_id = f"chunk_{second_chunks[0].id}"
        preserved_vector_id = f"chunk_{second_chunks[1].id}"

    collection.delete(ids=[missing_vector_id])
    preserved_before = dict(collection.records[preserved_vector_id])

    _index(isolated_environment["session_factory"], first_id)
    with isolated_environment["session_factory"]() as db:
        targeted = embedding_service.reconcile_index_consistency(
            db,
            paper_ids=[first_id],
        )

    assert targeted["consistent"] is True
    assert collection.records[preserved_vector_id] == preserved_before
    audit = _audit(isolated_environment, collection)
    assert audit["missing_vector_count"] == 1
    assert audit["affected_paper_ids"] == [second_id]
    assert first_id not in audit["affected_paper_ids"]


def test_vector_read_failure_is_unverifiable_not_consistent(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "read-failure.pdf",
        "Vector read failure evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    collection.fail_next_get_ids = True

    audit = _audit(isolated_environment, collection)
    assert audit["consistent"] is False
    assert audit["unverifiable_vector_count"] == 1
    assert audit["read_error_count"] == 1


def test_orphan_delete_noop_is_not_reported_as_success(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "orphan-noop.pdf",
        "Valid paper vector before no-op orphan deletion.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    collection.records["chunk_999998"] = {
        "embedding": [8.0, 8.0],
        "document": "orphan that Chroma refuses to delete",
        "metadata": {"paper_id": 999998, "chunk_id": 999998},
    }
    collection.delete_noop_ids = {"chunk_999998"}

    with isolated_environment["session_factory"]() as db:
        with pytest.raises(RuntimeError, match="orphan deletion was incomplete"):
            embedding_service.reconcile_index_consistency(
                db,
                paper_ids=[],
            )


def test_partial_orphan_delete_is_not_reported_as_success(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "orphan-partial.pdf",
        "Valid paper vector before partial orphan deletion.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    for chunk_id in (999990, 999991):
        collection.records[f"chunk_{chunk_id}"] = {
            "embedding": [7.0, 7.0],
            "document": f"orphan {chunk_id}",
            "metadata": {"paper_id": chunk_id, "chunk_id": chunk_id},
        }
    collection.delete_only_ids = {"chunk_999990"}

    with isolated_environment["session_factory"]() as db:
        with pytest.raises(RuntimeError, match="orphan deletion was incomplete"):
            embedding_service.reconcile_index_consistency(
                db,
                paper_ids=[],
            )
    assert "chunk_999991" in collection.records


def test_reconciliation_only_rechecks_global_orphans_after_cleanup(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "orphan-global-recheck.pdf",
        "Global orphan recheck evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    collection.records["chunk_999995"] = {
        "embedding": [6.5, 6.5],
        "document": "initial orphan",
        "metadata": {"paper_id": 999995, "chunk_id": 999995},
    }
    original_cleanup = embedding_service._delete_orphaned_index_vectors

    def cleanup_then_spawn_orphan(db, fake_collection, batch_size):
        result = original_cleanup(db, fake_collection, batch_size)
        fake_collection.records["chunk_999997"] = {
            "embedding": [6.0, 6.0],
            "document": "late orphan",
            "metadata": {"paper_id": 999997, "chunk_id": 999997},
        }
        return result

    monkeypatch.setattr(
        embedding_service,
        "_delete_orphaned_index_vectors",
        cleanup_then_spawn_orphan,
    )

    with isolated_environment["session_factory"]() as db:
        with pytest.raises(RuntimeError, match="did not converge"):
            embedding_service.reconcile_index_consistency(
                db,
                paper_ids=[],
            )
    assert "chunk_999997" in collection.records


def test_coordinated_chunk_creation_cannot_race_orphan_delete(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "orphan-race.pdf",
        "Coordinated orphan race evidence.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)
    collection.records["chunk_999996"] = {
        "embedding": [5.0, 5.0],
        "document": "old orphan",
        "metadata": {"paper_id": 999996, "chunk_id": 999996},
    }
    delete_entered = threading.Event()
    release_delete = threading.Event()
    creation_started = threading.Event()
    creation_finished = threading.Event()
    errors = []

    def block_delete(ids=None, where=None):
        if ids and "chunk_999996" in ids:
            delete_entered.set()
            assert release_delete.wait(5)

    collection.delete_hook = block_delete

    def cleanup_worker():
        try:
            with isolated_environment["session_factory"]() as db:
                embedding_service._delete_orphaned_index_vectors(
                    db,
                    collection,
                    batch_size=2,
                )
        except Exception as exc:
            errors.append(exc)

    def creation_worker():
        creation_started.set()
        try:
            with paper_operation_coordinator.collection_mutation():
                with isolated_environment["session_factory"]() as db:
                    db.add(
                        PaperChunk(
                            id=999996,
                            paper_id=paper_id,
                            page_number=1,
                            chunk_index=999996,
                            text="new coordinated chunk",
                            content_type="paragraph",
                        )
                    )
                    db.commit()
                collection.records["chunk_999996"] = {
                    "embedding": [4.0, 4.0],
                    "document": "new coordinated chunk",
                    "metadata": {"paper_id": paper_id, "chunk_id": 999996},
                }
        except Exception as exc:
            errors.append(exc)
        finally:
            creation_finished.set()

    cleanup_thread = threading.Thread(target=cleanup_worker)
    cleanup_thread.start()
    assert delete_entered.wait(5)
    creation_thread = threading.Thread(target=creation_worker)
    creation_thread.start()
    assert creation_started.wait(5)
    assert not creation_finished.wait(0.05)

    release_delete.set()
    cleanup_thread.join(5)
    creation_thread.join(5)

    assert not cleanup_thread.is_alive()
    assert not creation_thread.is_alive()
    assert creation_finished.is_set()
    assert not errors
    assert "chunk_999996" in collection.records
    with isolated_environment["session_factory"]() as db:
        assert db.query(PaperChunk).filter_by(id=999996).one()


def test_schema_2_vector_upgrades_without_rebuilding_other_chunks(
    isolated_environment,
    monkeypatch,
):
    paper_id = _upload(
        isolated_environment["client"],
        "schema-two.pdf",
        "First schema upgrade retrieval chunk.",
    )
    _add_chunk(
        isolated_environment["session_factory"],
        paper_id,
        "Second already current retrieval chunk.",
    )
    collection = ContentAwareFakeCollection()
    _configure_indexer(monkeypatch, collection)
    _index(isolated_environment["session_factory"], paper_id)

    with isolated_environment["session_factory"]() as db:
        chunks = (
            db.query(PaperChunk)
            .filter_by(paper_id=paper_id)
            .order_by(
                PaperChunk.page_number.asc(),
                PaperChunk.page_chunk_index.asc(),
            )
            .all()
        )
        assert len(chunks) == 2
        old_chunk = chunks[0]
        current_chunk = chunks[1]
        old_vector_id = f"chunk_{old_chunk.id}"
        current_vector_id = f"chunk_{current_chunk.id}"
        old_record = collection.records[old_vector_id]
        old_metadata = dict(old_record["metadata"])
        old_metadata.pop("page_chunk_index")
        old_metadata["index_schema_version"] = 2
        old_key_metadata = {
            field: old_metadata.get(field)
            for field in (
                "chunk_id",
                "paper_id",
                "paper_title",
                "page_number",
                "content_type",
                "element_id",
            )
        }
        payload = {
            "content_hash": old_metadata["content_hash"],
            "content_hash_algorithm": old_metadata["content_hash_algorithm"],
            "content_normalization_version": old_metadata[
                "content_normalization_version"
            ],
            "index_schema_version": 2,
            "metadata": old_key_metadata,
        }
        old_metadata["index_fingerprint"] = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        old_record["metadata"] = old_metadata

    stale = _audit(isolated_environment, collection)
    assert stale["stale_vector_count"] == 1
    assert old_vector_id in stale["stale_vector_ids"]
    assert stale["affected_paper_ids"] == [paper_id]
    reasons = next(
        issue["reasons"]
        for issue in stale["issues"]
        if issue["vector_id"] == old_vector_id
    )
    assert "index_schema_version_mismatch" in reasons
    assert "metadata_changed:page_chunk_index" in reasons

    encoded_texts = []

    def encode(texts):
        encoded_texts.extend(texts)
        return [[1.0, float(index)] for index, _ in enumerate(texts)]

    monkeypatch.setattr(embedding_service, "encode_texts", encode)
    _index(isolated_environment["session_factory"], paper_id)

    assert encoded_texts == [old_chunk.text.strip()]
    upgraded = collection.records[old_vector_id]["metadata"]
    assert upgraded["index_schema_version"] == 3
    assert upgraded["page_number"] == old_chunk.page_number
    assert upgraded["page_chunk_index"] == old_chunk.page_chunk_index
    assert old_vector_id == f"chunk_{upgraded['chunk_id']}"
    assert current_vector_id in collection.records
    assert _audit(isolated_environment, collection)["consistent"] is True
