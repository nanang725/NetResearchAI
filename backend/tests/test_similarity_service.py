from __future__ import annotations

from app.models.paper import Paper, PaperChunk
from app.services import embedding_service


class FakeCollection:
    def __init__(
        self,
        ids: list[str],
        metadata: list[dict] | None = None,
        documents: list[str] | None = None,
    ):
        self.ids = ids
        self.metadata = metadata or []
        self.documents = documents or [""] * len(ids)

    def count(self):
        return len(self.ids)

    def query(self, **_kwargs):
        return {
            "metadatas": [self.metadata],
            "distances": [[0.05] * len(self.metadata)],
        }

    def get(self, ids=None, limit=None, offset=0, include=None):
        del include
        selected = [
            index
            for index, vector_id in enumerate(self.ids)
            if ids is None or vector_id in set(ids)
        ]
        end = None if limit is None else offset + limit
        selected = selected[offset:end]
        result = {"ids": [self.ids[index] for index in selected]}
        result["documents"] = [self.documents[index] for index in selected]
        result["metadatas"] = [self.metadata[index] for index in selected]
        return result

    def delete(self, ids):
        remove = set(ids)
        retained = [
            index for index, vector_id in enumerate(self.ids)
            if vector_id not in remove
        ]
        self.ids = [self.ids[index] for index in retained]
        self.documents = [self.documents[index] for index in retained]
        self.metadata = [self.metadata[index] for index in retained]


def test_similarity_normalization_preserves_chinese_text():
    assert embedding_service._normalize_similarity_text("中文 Paper v2") == "中文paperv2"


def test_bge_candidate_is_explainable_and_not_automatically_grouped(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    first = client.post(
        "/api/papers/upload",
        files={"file": ("first.pdf", _pdf("网络拥塞控制研究"), "application/pdf")},
    ).json()["data"]
    second = client.post(
        "/api/papers/upload",
        files={"file": ("second.pdf", _pdf("网络拥塞控制研究修订版"), "application/pdf")},
    ).json()["data"]
    collection = FakeCollection(
        ["chunk_1"],
        [{"paper_id": first["id"], "paper_title": first["title"]}],
    )
    monkeypatch.setattr(embedding_service, "get_chroma_collection", lambda: collection)
    monkeypatch.setattr(embedding_service, "encode_texts", lambda _texts: [[1.0, 0.0]])

    with isolated_environment["session_factory"]() as db:
        candidates = embedding_service.find_similar_papers(db, second["id"])
        assert candidates[0]["paper_id"] == first["id"]
        assert candidates[0]["semantic_similarity"] == 0.95
        assert "BGE-M3" in candidates[0]["match_reason"]
        assert candidates[0]["candidate_type"] in {"suspected_version", "possible_duplicate"}
        assert db.query(Paper).filter_by(id=second["id"]).one().version is None


def test_metadata_candidate_is_returned_without_vectors_or_automatic_linking(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    first = client.post(
        "/api/papers/upload",
        files={
            "file": (
                "first-metadata.pdf",
                _metadata_pdf("Version A"),
                "application/pdf",
            )
        },
    ).json()["data"]
    second = client.post(
        "/api/papers/upload",
        files={
            "file": (
                "second-metadata.pdf",
                _metadata_pdf("Version B"),
                "application/pdf",
            )
        },
    ).json()["data"]
    monkeypatch.setattr(embedding_service, "get_chroma_collection", lambda: FakeCollection([]))

    with isolated_environment["session_factory"]() as db:
        candidates = embedding_service.find_similar_papers(db, second["id"])
        candidate = next(item for item in candidates if item["paper_id"] == first["id"])
        assert candidate["metadata_similarity"] == 1.0
        assert "DOI 一致" in candidate["match_reason"]
        assert candidate["candidate_type"] == "suspected_version"
        assert db.query(Paper).filter_by(id=first["id"]).one().version is None
        assert db.query(Paper).filter_by(id=second["id"]).one().version is None


def test_index_audit_reports_and_removes_only_orphaned_vectors(
    isolated_environment,
    monkeypatch,
):
    client = isolated_environment["client"]
    paper = client.post(
        "/api/papers/upload",
        files={"file": ("audit.pdf", _pdf("索引一致性测试"), "application/pdf")},
    ).json()["data"]
    with isolated_environment["session_factory"]() as db:
        records = (
            db.query(PaperChunk, Paper)
            .join(Paper, Paper.id == PaperChunk.paper_id)
            .filter(PaperChunk.paper_id == paper["id"])
            .all()
        )
        built_by_id = {
            f"chunk_{chunk.id}": embedding_service.build_chunk_index_record(
                chunk,
                paper_record,
            )
            for chunk, paper_record in records
        }
        expected = {f"chunk_{chunk.id}" for chunk, _paper in records}
        ordered_ids = sorted(expected)
        collection = FakeCollection(ordered_ids + ["orphan_vector"])
        collection.documents = [
            built_by_id[vector_id][0] for vector_id in ordered_ids
        ] + [""]
        collection.metadata = [
            built_by_id[vector_id][1] for vector_id in ordered_ids
        ] + [{"paper_id": paper["id"]}]
        monkeypatch.setattr(embedding_service, "get_chroma_collection", lambda: collection)

        before = embedding_service.audit_index_consistency(db, batch_size=1)
        assert before["consistent"] is False
        assert before["missing_vector_count"] == 0
        assert before["orphan_vector_ids"] == ["orphan_vector"]

        repaired = embedding_service.reconcile_index_consistency(db, batch_size=1)
        assert repaired["consistent"] is True
        assert collection.ids == sorted(expected)


def _pdf(text: str) -> bytes:
    import io

    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s")
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _metadata_pdf(version: str) -> bytes:
    import io

    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A Study of Network Reliability", fontsize=18)
    page.insert_text((72, 110), "DOI: 10.1234/Net.2024.001", fontsize=10)
    page.insert_text((72, 140), "Jane Doe, 2024", fontsize=10)
    page.insert_text((72, 180), version, fontsize=11)
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()
