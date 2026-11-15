from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import chromadb
import fitz
import pytest
from chromadb.config import Settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - register every model on Base.metadata
from app.config import get_settings
from app.database import Base
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.services import embedding_service
from app.services.paper_service import (
    create_or_resume_paper,
    get_paper_parse_completeness,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
MANIFEST_PATH = Path(__file__).parent / "fixtures" / "real_pdf_manifest.json"
DEFAULT_REPORT_PATH = (
    BACKEND_ROOT / ".pytest_cache" / "real_pdf_regression_report.json"
)


@dataclass(frozen=True)
class RealPdfCase:
    id: str
    path: Path
    source_kind: str
    title: str
    sha256: str
    expected_pages: int
    identity_tokens: tuple[str, ...]
    minimum_total_chars: int
    minimum_chunk_count: int
    minimum_math_text_lines: int
    minimum_element_types: dict[str, int]
    maximum_element_types: dict[str, int]
    required_text_tokens: tuple[str, ...]
    focus: str


@dataclass
class SampleResult:
    sample_id: str
    status: str
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegressionWorkspace:
    engine: Any
    session_factory: Any
    upload_dir: Path
    chroma_dir: Path
    chroma_client: Any
    collection: Any
    encoder: "TrackedEmbedding"
    results: dict[str, SampleResult]
    global_audit: dict[str, Any]
    manifest: dict[str, Any]
    report_path: Path
    embedding_mode: str
    started_perf: float


class TrackedEmbedding:
    def __init__(
        self,
        *,
        mode: str,
        real_encoder: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        self.mode = mode
        self.real_encoder = real_encoder
        self.calls: list[int] = []

    @property
    def encoded_text_count(self) -> int:
        return sum(self.calls)

    def reset(self) -> None:
        self.calls.clear()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        if self.mode == "real_bge_m3":
            if self.real_encoder is None:
                raise RuntimeError("Real embedding mode has no base encoder")
            return self.real_encoder(texts)
        return [self._deterministic_vector(text) for text in texts]

    @staticmethod
    def _deterministic_vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [
            int.from_bytes(digest[index:index + 2], "big") / 65535.0
            for index in range(0, 16, 2)
        ]
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [value / norm for value in values]


def _load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise AssertionError("Unsupported real PDF manifest version")
    ids = [sample["id"] for sample in manifest["samples"]]
    if len(ids) != len(set(ids)):
        raise AssertionError("Real PDF manifest contains duplicate sample IDs")
    return manifest


def _case_from_manifest(sample: dict[str, Any]) -> RealPdfCase:
    return RealPdfCase(
        id=str(sample["id"]),
        path=(PROJECT_ROOT / sample["path"]).resolve(),
        source_kind=str(sample["source_kind"]),
        title=str(sample["title"]),
        sha256=str(sample["sha256"]),
        expected_pages=int(sample["expected_pages"]),
        identity_tokens=tuple(str(value) for value in sample["identity_tokens"]),
        minimum_total_chars=int(sample["minimum_total_chars"]),
        minimum_chunk_count=int(sample["minimum_chunk_count"]),
        minimum_math_text_lines=int(
            sample.get("minimum_math_text_lines", 0)
        ),
        minimum_element_types={
            str(key): int(value)
            for key, value in sample["minimum_element_types"].items()
        },
        maximum_element_types={
            str(key): int(value)
            for key, value in sample.get("maximum_element_types", {}).items()
        },
        required_text_tokens=tuple(
            str(value) for value in sample["required_text_tokens"]
        ),
        focus=str(sample["focus"]),
    )


def _normalize_for_identity(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_pdf_identity(path: Path) -> tuple[int, str]:
    document = fitz.open(path)
    try:
        if document.page_count <= 0:
            raise AssertionError(f"{path} has no pages")
        first_page_text = document[0].get_text("text")
        return document.page_count, first_page_text
    finally:
        document.close()


def _make_collection(path: Path, name: str):
    path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(path),
        settings=Settings(anonymized_telemetry=False, allow_reset=False),
    )
    collection = client.get_or_create_collection(
        name=name,
        embedding_function=None,
        metadata={
            "description": "isolated NetResearch AI real PDF regression",
            "hnsw:space": "cosine",
        },
    )
    return client, collection


def _create_database_environment(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    real_embedding: bool,
    resources: dict[str, Any] | None = None,
) -> tuple[Any, Any, Path, Path, Any, Any, TrackedEmbedding, str]:
    database_path = root / "real_pdf_regression.db"
    upload_dir = root / "uploads"
    chroma_dir = root / "chroma"
    upload_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("MAX_UPLOAD_MB", "30")
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    if resources is not None:
        resources["engine"] = engine
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )

    chroma_client, collection = _make_collection(
        chroma_dir,
        "netresearch_real_pdf_regression",
    )
    if resources is not None:
        resources["chroma_client"] = chroma_client
    real_encoder = embedding_service.encode_texts if real_embedding else None
    mode = "real_bge_m3" if real_embedding else "deterministic_test_embedding"
    encoder = TrackedEmbedding(mode=mode, real_encoder=real_encoder)
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(embedding_service, "encode_texts", encoder)
    return (
        engine,
        session_factory,
        upload_dir,
        chroma_dir,
        chroma_client,
        collection,
        encoder,
        mode,
    )


def _database_state(session_factory, paper_id: int) -> dict[str, Any]:
    with session_factory() as db:
        paper = db.query(Paper).filter(Paper.id == paper_id).one()
        pages = (
            db.query(PaperPage)
            .filter(PaperPage.paper_id == paper_id)
            .order_by(PaperPage.page_number.asc())
            .all()
        )
        elements = (
            db.query(PaperElement)
            .filter(PaperElement.paper_id == paper_id)
            .order_by(
                PaperElement.page_number.asc(),
                PaperElement.element_index.asc(),
            )
            .all()
        )
        chunks = (
            db.query(PaperChunk)
            .filter(PaperChunk.paper_id == paper_id)
            .order_by(
                PaperChunk.page_number.asc(),
                PaperChunk.page_chunk_index.asc(),
                PaperChunk.chunk_index.asc(),
                PaperChunk.id.asc(),
            )
            .all()
        )
        completeness = get_paper_parse_completeness(db, paper_id)
        paper_count = db.query(Paper).count()
        return {
            "paper": {
                "id": int(paper.id),
                "title": paper.title,
                "authors": paper.authors,
                "parse_status": paper.parse_status,
                "index_status": paper.index_status,
                "page_count": int(paper.page_count),
                "char_count": int(paper.char_count),
                "parse_source_hash": paper.parse_source_hash,
                "parse_source_page_count": int(paper.parse_source_page_count),
            },
            "paper_count": paper_count,
            "pages": [
                {
                    "id": int(page.id),
                    "page_number": int(page.page_number),
                    "text": page.text,
                    "char_count": int(page.char_count),
                    "parse_status": page.parse_status,
                    "checkpoint_status": page.checkpoint_status,
                }
                for page in pages
            ],
            "elements": [
                {
                    "id": int(element.id),
                    "page_number": int(element.page_number),
                    "element_index": int(element.element_index),
                    "element_type": element.element_type,
                    "text": element.text,
                }
                for element in elements
            ],
            "chunks": [
                {
                    "id": int(chunk.id),
                    "page_number": int(chunk.page_number),
                    "page_chunk_index": int(chunk.page_chunk_index),
                    "chunk_index": int(chunk.chunk_index),
                    "text": chunk.text,
                    "content_type": chunk.content_type,
                }
                for chunk in chunks
            ],
            "completeness": {
                "complete": completeness.complete,
                "expected_page_count": completeness.expected_page_count,
                "recorded_page_count": completeness.recorded_page_count,
                "completed_page_count": completeness.completed_page_count,
                "failed_page_count": completeness.failed_page_count,
                "processing_page_count": completeness.processing_page_count,
                "pending_page_count": completeness.pending_page_count,
                "missing_page_count": completeness.missing_page_count,
            },
        }


def _chunk_snapshot(state: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            chunk["id"],
            chunk["page_number"],
            chunk["page_chunk_index"],
            chunk["chunk_index"],
            chunk["text"],
            chunk["content_type"],
        )
        for chunk in state["chunks"]
    ]


def _count_math_text_lines(pages: list[dict[str, Any]]) -> int:
    count = 0
    for page in pages:
        for line in page["text"].splitlines():
            value = line.strip()
            if len(value) < 4:
                continue
            operator_count = sum(
                value.count(character)
                for character in "=∑∫√≈≤≥×÷±∏∂"
            )
            if (
                "=" in value
                and re.search(r"[A-Za-z0-9λμσ]", value)
            ) or operator_count >= 2:
                count += 1
    return count


def _assert_chunk_order(state: dict[str, Any], expected_pages: int) -> None:
    chunks = state["chunks"]
    assert chunks, "No chunks were generated"
    assert len({chunk["id"] for chunk in chunks}) == len(chunks)
    assert len({chunk["chunk_index"] for chunk in chunks}) == len(chunks)
    keys = [
        (
            chunk["page_number"],
            chunk["page_chunk_index"],
            chunk["chunk_index"],
            chunk["id"],
        )
        for chunk in chunks
    ]
    assert keys == sorted(keys), "Chunks are not in global page order"
    assert all(1 <= chunk["page_number"] <= expected_pages for chunk in chunks)
    assert all(chunk["text"].strip() for chunk in chunks)
    assert all(len(chunk["text"].strip()) >= 10 for chunk in chunks)

    by_page: dict[int, list[dict[str, Any]]] = {}
    for chunk in chunks:
        by_page.setdefault(chunk["page_number"], []).append(chunk)
    for page_number, page_chunks in by_page.items():
        assert [chunk["page_chunk_index"] for chunk in page_chunks] == list(
            range(len(page_chunks))
        ), f"page_chunk_index is not contiguous on page {page_number}"
        assert [chunk["chunk_index"] for chunk in page_chunks] == sorted(
            chunk["chunk_index"] for chunk in page_chunks
        ), f"chunk_index is not stable on page {page_number}"


def _assert_vectors_against_database(
    session_factory,
    collection,
    paper_id: int,
) -> set[str]:
    with session_factory() as db:
        paper = db.query(Paper).filter(Paper.id == paper_id).one()
        chunks = (
            db.query(PaperChunk)
            .filter(PaperChunk.paper_id == paper_id)
            .order_by(
                PaperChunk.page_number.asc(),
                PaperChunk.page_chunk_index.asc(),
                PaperChunk.chunk_index.asc(),
                PaperChunk.id.asc(),
            )
            .all()
        )
        expected = {
            f"chunk_{chunk.id}": embedding_service.build_chunk_index_record(
                chunk,
                paper,
            )
            for chunk in chunks
        }

    response = collection.get(
        ids=list(expected),
        include=["documents", "metadatas"],
    )
    ids = list(response.get("ids", []))
    documents = response.get("documents", [])
    metadatas = response.get("metadatas", [])
    assert set(ids) == set(expected)
    actual = {
        vector_id: (
            documents[index] if index < len(documents) else None,
            metadatas[index] if index < len(metadatas) else None,
        )
        for index, vector_id in enumerate(ids)
    }
    for vector_id, (expected_document, expected_metadata) in expected.items():
        actual_document, actual_metadata = actual[vector_id]
        assert actual_document == expected_document
        assert actual_metadata == expected_metadata
        for field_name in (
            "content_hash",
            "content_hash_algorithm",
            "content_normalization_version",
            "index_fingerprint",
            "index_schema_version",
            "page_number",
            "page_chunk_index",
        ):
            assert actual_metadata[field_name] == expected_metadata[field_name]
    return set(expected)


def _audit_metrics(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "sqlite_chunk_count": int(audit["sqlite_chunk_count"]),
        "chroma_vector_count": int(audit["chroma_vector_count"]),
        "missing_vector_count": int(audit["missing_vector_count"]),
        "stale_vector_count": int(audit["stale_vector_count"]),
        "legacy_vector_count": int(audit["legacy_vector_count"]),
        "orphan_vector_count": int(audit["orphan_vector_count"]),
        "unverifiable_vector_count": int(audit["unverifiable_vector_count"]),
        "parse_incomplete_page_count": int(
            audit["parse_incomplete_page_count"]
        ),
        "consistent": bool(audit["consistent"]),
    }


def _assert_clean_audit(audit: dict[str, Any]) -> None:
    assert audit["consistent"] is True
    assert audit["missing_vector_count"] == 0
    assert audit["stale_vector_count"] == 0
    assert audit["legacy_vector_count"] == 0
    assert audit["unverifiable_vector_count"] == 0
    assert audit["parse_incomplete_page_count"] == 0


def _run_sample(
    case: RealPdfCase,
    *,
    session_factory,
    upload_dir: Path,
    collection,
    encoder: TrackedEmbedding,
    embedding_mode: str,
) -> SampleResult:
    started = time.perf_counter()
    metrics: dict[str, Any] = {
        "file_exists": case.path.is_file(),
        "expected_pages": case.expected_pages,
        "embedding_mode": embedding_mode,
        "focus": case.focus,
        "source_kind": case.source_kind,
    }
    if not case.path.is_file():
        return SampleResult(
            sample_id=case.id,
            status="SKIPPED",
            reason=f"Missing real PDF: {case.path}",
            metrics=metrics,
        )

    try:
        actual_sha256 = _sha256_file(case.path)
        metrics["sha256"] = actual_sha256
        assert actual_sha256 == case.sha256, "SHA-256 does not match manifest"

        pdf_pages, first_page_text = _read_pdf_identity(case.path)
        metrics["pdf_page_count"] = pdf_pages
        normalized_first_page = _normalize_for_identity(first_page_text)
        identity_ok = all(
            _normalize_for_identity(token) in normalized_first_page
            for token in case.identity_tokens
        )
        metrics["identity_verified"] = identity_ok
        assert identity_ok, "First page does not contain all identity tokens"
        assert pdf_pages == case.expected_pages

        content = case.path.read_bytes()
        with session_factory() as db:
            paper = create_or_resume_paper(
                db,
                filename=case.path.name,
                content=content,
                content_type="application/pdf",
            )
            paper_id = int(paper.id)
        metrics["paper_id"] = paper_id

        first_state = _database_state(session_factory, paper_id)
        pages = first_state["pages"]
        page_numbers = [page["page_number"] for page in pages]
        assert page_numbers == list(range(1, case.expected_pages + 1))
        assert len({page["id"] for page in pages}) == case.expected_pages
        assert all(page["checkpoint_status"] == "completed" for page in pages)
        assert all(page["text"].strip() for page in pages)
        completeness = first_state["completeness"]
        assert completeness["complete"] is True
        assert completeness["expected_page_count"] == case.expected_pages
        assert completeness["recorded_page_count"] == case.expected_pages
        assert completeness["completed_page_count"] == case.expected_pages
        assert completeness["failed_page_count"] == 0
        assert completeness["processing_page_count"] == 0
        assert completeness["pending_page_count"] == 0
        assert completeness["missing_page_count"] == 0

        paper_row = first_state["paper"]
        assert paper_row["parse_source_hash"] == case.sha256
        assert paper_row["page_count"] == case.expected_pages
        assert paper_row["char_count"] >= case.minimum_total_chars
        assert len(first_state["chunks"]) >= case.minimum_chunk_count
        _assert_chunk_order(first_state, case.expected_pages)

        element_types = Counter(
            element["element_type"] for element in first_state["elements"]
        )
        for element_type, minimum in case.minimum_element_types.items():
            assert element_types[element_type] >= minimum
        for element_type, maximum in case.maximum_element_types.items():
            assert element_types[element_type] <= maximum
        page_text = "\n".join(page["text"] for page in pages).casefold()
        for token in case.required_text_tokens:
            assert token.casefold() in page_text
        math_text_line_count = _count_math_text_lines(pages)
        assert math_text_line_count >= case.minimum_math_text_lines

        before_repeat_chunks = _chunk_snapshot(first_state)
        with session_factory() as db:
            create_or_resume_paper(
                db,
                filename=case.path.name,
                content=content,
                content_type="application/pdf",
            )
        repeated_state = _database_state(session_factory, paper_id)
        assert _chunk_snapshot(repeated_state) == before_repeat_chunks
        with session_factory() as db:
            matching_paper_count = (
                db.query(Paper)
                .filter(Paper.file_hash == case.sha256)
                .count()
            )
        assert matching_paper_count == 1
        stored_pdf_count = len(list(upload_dir.glob("*.pdf")))

        encoder.reset()
        with session_factory() as db:
            first_index = embedding_service.index_paper(db, paper_id)
        first_embedding_input_count = encoder.encoded_text_count
        assert first_embedding_input_count == len(first_state["chunks"])
        assert first_index["indexed_count"] == len(first_state["chunks"])
        with session_factory() as db:
            first_audit = embedding_service.audit_paper_index_consistency(
                db,
                paper_id,
                batch_size=37,
            )
        _assert_clean_audit(first_audit)
        first_vector_ids = _assert_vectors_against_database(
            session_factory,
            collection,
            paper_id,
        )

        with session_factory() as db:
            create_or_resume_paper(
                db,
                filename=case.path.name,
                content=content,
                content_type="application/pdf",
            )
        encoder.reset()
        with session_factory() as db:
            second_index = embedding_service.index_paper(db, paper_id)
        second_embedding_input_count = encoder.encoded_text_count
        with session_factory() as db:
            final_audit = embedding_service.audit_paper_index_consistency(
                db,
                paper_id,
                batch_size=37,
            )
        _assert_clean_audit(final_audit)
        final_state = _database_state(session_factory, paper_id)
        assert _chunk_snapshot(final_state) == before_repeat_chunks
        final_vector_ids = _assert_vectors_against_database(
            session_factory,
            collection,
            paper_id,
        )
        assert final_vector_ids == first_vector_ids
        assert second_index["indexed_count"] == len(final_state["chunks"])
        assert second_embedding_input_count == 0
        assert len(list(upload_dir.glob("*.pdf"))) == stored_pdf_count
        _assert_chunk_order(final_state, case.expected_pages)

        expected_title = case.title
        parsed_title = final_state["paper"]["title"] or ""
        parsed_title_matches = (
            _normalize_for_identity(expected_title)
            in _normalize_for_identity(parsed_title)
        )
        assert parsed_title_matches, (
            f"Parsed title does not match manifest: {parsed_title!r}"
        )
        metrics.update(
            {
                "parsed_title": parsed_title,
                "expected_title": expected_title,
                "parsed_title_matches_manifest": parsed_title_matches,
                "actual_pages": pdf_pages,
                "page_records": len(pages),
                "completed_pages": completeness["completed_page_count"],
                "failed_pages": completeness["failed_page_count"],
                "processing_pages": completeness["processing_page_count"],
                "pending_pages": completeness["pending_page_count"],
                "missing_pages": completeness["missing_page_count"],
                "total_chars": int(final_state["paper"]["char_count"]),
                "page_char_lengths": [
                    int(page["char_count"]) for page in pages
                ],
                "structure_element_count": len(final_state["elements"]),
                "structure_element_types": dict(element_types),
                "formula_element_count": int(element_types.get("formula", 0)),
                "math_text_line_count": math_text_line_count,
                "chunk_count": len(final_state["chunks"]),
                "chunk_order": "PASS",
                "chunk_page_counts": dict(
                    Counter(
                        chunk["page_number"]
                        for chunk in final_state["chunks"]
                    )
                ),
                "chroma_vector_count": len(final_vector_ids),
                "audit": _audit_metrics(final_audit),
                "first_embedding_input_count": first_embedding_input_count,
                "second_embedding_input_count": second_embedding_input_count,
                "idempotent": (
                    second_embedding_input_count == 0
                    and final_vector_ids == first_vector_ids
                ),
                "stored_pdf_count": stored_pdf_count,
            }
        )
        metrics["duration_seconds"] = round(
            time.perf_counter() - started,
            3,
        )
        return SampleResult(
            sample_id=case.id,
            status="PASS",
            metrics=metrics,
        )
    except Exception as exc:
        metrics["duration_seconds"] = round(
            time.perf_counter() - started,
            3,
        )
        return SampleResult(
            sample_id=case.id,
            status="FAIL",
            reason=f"{type(exc).__name__}: {exc}",
            metrics=metrics,
        )


def _report_payload(workspace: RegressionWorkspace) -> dict[str, Any]:
    samples = [result for result in workspace.results.values()]
    passed = [result for result in samples if result.status == "PASS"]
    failed = [result for result in samples if result.status == "FAIL"]
    skipped = [result for result in samples if result.status == "SKIPPED"]
    totals = {
        "sample_count": len(samples),
        "passed_sample_count": len(passed),
        "failed_sample_count": len(failed),
        "skipped_sample_count": len(skipped),
        "total_pages": sum(
            int(result.metrics.get("actual_pages", 0)) for result in passed
        ),
        "total_chunks": sum(
            int(result.metrics.get("chunk_count", 0)) for result in passed
        ),
        "total_vectors": sum(
            int(result.metrics.get("chroma_vector_count", 0))
            for result in passed
        ),
        "total_missing_vectors": sum(
            int(result.metrics.get("audit", {}).get("missing_vector_count", 0))
            for result in passed
        ),
        "total_stale_vectors": sum(
            int(result.metrics.get("audit", {}).get("stale_vector_count", 0))
            for result in passed
        ),
        "total_orphan_vectors": int(
            workspace.global_audit.get("orphan_vector_count", 0)
        ),
        "total_duration_seconds": round(
            time.perf_counter() - workspace.started_perf,
            3,
        ),
        "global_consistency": bool(workspace.global_audit.get("consistent")),
        "global_audit": _audit_metrics(workspace.global_audit),
    }
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "manifest": str(MANIFEST_PATH),
        "embedding_mode": workspace.embedding_mode,
        "embedding_note": (
            "BGE-M3 inference was run with the existing local model cache."
            if workspace.embedding_mode == "real_bge_m3"
            else (
                "Deterministic 8-dimensional test embeddings exercise SQLite, "
                "production indexing and real Chroma persistence. They do not "
                "measure BGE-M3 model quality."
            )
        ),
        "totals": totals,
        "samples": {
            result.sample_id: {
                "status": result.status,
                "reason": result.reason,
                **result.metrics,
            }
            for result in samples
        },
    }


def _write_report(workspace: RegressionWorkspace) -> None:
    workspace.report_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.report_path.write_text(
        json.dumps(
            _report_payload(workspace),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def real_pdf_workspace(request, tmp_path_factory):
    manifest = _load_manifest()
    requested_ids = {
        str(value).upper() for value in request.config.getoption("--real-pdf-id")
    }
    known_ids = {str(sample["id"]) for sample in manifest["samples"]}
    unknown_ids = requested_ids - known_ids
    if unknown_ids:
        raise pytest.UsageError(
            f"Unknown real PDF sample IDs: {sorted(unknown_ids)}"
        )

    cases = [
        _case_from_manifest(sample)
        for sample in manifest["samples"]
        if not requested_ids or str(sample["id"]) in requested_ids
    ]
    root = tmp_path_factory.mktemp("real_pdf_regression")
    monkeypatch = pytest.MonkeyPatch()
    resources: dict[str, Any] = {}
    real_embedding = bool(
        request.config.getoption("--run-real-embedding")
    )
    try:
        (
            engine,
            session_factory,
            upload_dir,
            chroma_dir,
            chroma_client,
            collection,
            encoder,
            embedding_mode,
        ) = _create_database_environment(
            root,
            monkeypatch,
            real_embedding=real_embedding,
            resources=resources,
        )
        report_option = request.config.getoption("--real-pdf-report")
        report_path = (
            Path(report_option).expanduser()
            if report_option
            else DEFAULT_REPORT_PATH
        )
        if not report_path.is_absolute():
            report_path = (BACKEND_ROOT / report_path).resolve()

        workspace = RegressionWorkspace(
            engine=engine,
            session_factory=session_factory,
            upload_dir=upload_dir,
            chroma_dir=chroma_dir,
            chroma_client=chroma_client,
            collection=collection,
            encoder=encoder,
            results={},
            global_audit={},
            manifest=manifest,
            report_path=report_path,
            embedding_mode=embedding_mode,
            started_perf=time.perf_counter(),
        )
        for case in cases:
            workspace.results[case.id] = _run_sample(
                case,
                session_factory=session_factory,
                upload_dir=upload_dir,
                collection=collection,
                encoder=encoder,
                embedding_mode=embedding_mode,
            )
        if requested_ids:
            for sample in manifest["samples"]:
                sample_id = str(sample["id"])
                if sample_id not in workspace.results:
                    workspace.results[sample_id] = SampleResult(
                        sample_id=sample_id,
                        status="SKIPPED",
                        reason="Sample was not selected by --real-pdf-id",
                    )
        with session_factory() as db:
            workspace.global_audit = embedding_service.audit_index_consistency(
                db,
                batch_size=37,
            )
        _write_report(workspace)
        yield workspace
    finally:
        chroma_client = resources.get("chroma_client")
        engine = resources.get("engine")
        try:
            if chroma_client is not None:
                chroma_client.close()
        finally:
            try:
                if engine is not None:
                    engine.dispose()
            finally:
                monkeypatch.undo()
                get_settings.cache_clear()


@pytest.mark.real_pdf
@pytest.mark.parametrize(
    "sample_id",
    [
        str(sample["id"])
        for sample in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))[
            "samples"
        ]
    ],
)
def test_real_pdf_parse_chunk_and_index_regression(
    real_pdf_workspace: RegressionWorkspace,
    sample_id: str,
):
    result = real_pdf_workspace.results[sample_id]
    if result.status == "SKIPPED":
        pytest.skip(result.reason)
    assert result.status == "PASS", result.reason

    metrics = result.metrics
    assert metrics["identity_verified"] is True
    assert metrics["actual_pages"] == metrics["expected_pages"]
    assert metrics["page_records"] == metrics["expected_pages"]
    assert metrics["completed_pages"] == metrics["expected_pages"]
    assert metrics["failed_pages"] == 0
    assert metrics["processing_pages"] == 0
    assert metrics["pending_pages"] == 0
    assert metrics["missing_pages"] == 0
    assert metrics["chunk_count"] > 0
    assert metrics["chunk_order"] == "PASS"
    assert metrics["audit"]["consistent"] is True
    assert metrics["audit"]["missing_vector_count"] == 0
    assert metrics["audit"]["stale_vector_count"] == 0
    assert metrics["audit"]["unverifiable_vector_count"] == 0
    assert metrics["idempotent"] is True
    assert metrics["second_embedding_input_count"] == 0


@pytest.mark.real_pdf
def test_real_pdf_global_audit_and_summary(
    real_pdf_workspace: RegressionWorkspace,
):
    payload = _report_payload(real_pdf_workspace)
    totals = payload["totals"]
    assert totals["failed_sample_count"] == 0
    if totals["passed_sample_count"] == 0:
        pytest.skip("No real PDF samples were available")
    assert totals["global_consistency"] is True
    assert totals["global_audit"]["missing_vector_count"] == 0
    assert totals["global_audit"]["stale_vector_count"] == 0
    assert totals["global_audit"]["legacy_vector_count"] == 0
    assert totals["global_audit"]["orphan_vector_count"] == 0
    assert totals["global_audit"]["unverifiable_vector_count"] == 0

    print("\n\nREAL PDF REGRESSION REPORT\n")
    print(
        "| ID | Status | Pages | Chars | Elements | Chunks | Vectors | "
        "Missing | Stale | Idempotent |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for sample_id, sample in payload["samples"].items():
        if sample["status"] == "PASS":
            audit = sample["audit"]
            print(
                f"| {sample_id} | PASS | {sample['actual_pages']} | "
                f"{sample['total_chars']} | "
                f"{sample['structure_element_count']} | "
                f"{sample['chunk_count']} | "
                f"{sample['chroma_vector_count']} | "
                f"{audit['missing_vector_count']} | "
                f"{audit['stale_vector_count']} | "
                f"{'PASS' if sample['idempotent'] else 'FAIL'} |"
            )
        else:
            print(f"| {sample_id} | {sample['status']} | - | - | - | - | - | - | - | - |")
    print(
        f"\nPASS={totals['passed_sample_count']} "
        f"FAIL={totals['failed_sample_count']} "
        f"SKIPPED={totals['skipped_sample_count']} "
        f"pages={totals['total_pages']} "
        f"chunks={totals['total_chunks']} "
        f"vectors={totals['total_vectors']} "
        f"missing={totals['total_missing_vectors']} "
        f"stale={totals['total_stale_vectors']} "
        f"orphaned={totals['total_orphan_vectors']}"
    )
    print(f"JSON report: {real_pdf_workspace.report_path}")
    assert real_pdf_workspace.report_path.is_file()


@pytest.mark.real_pdf
def test_real_pdf_middle_page_retry_restores_order(
    isolated_environment,
    tmp_path,
    monkeypatch,
    request,
):
    manifest = _load_manifest()
    sample = next(
        sample for sample in manifest["samples"] if sample["id"] == "P09"
    )
    case = _case_from_manifest(sample)
    if not case.path.is_file():
        pytest.skip(f"Missing real PDF: {case.path}")

    chroma_client, collection = _make_collection(
        tmp_path / "retry_chroma",
        "netresearch_real_pdf_retry",
    )
    request.addfinalizer(chroma_client.close)
    encoder = TrackedEmbedding(mode="deterministic_test_embedding")
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(embedding_service, "encode_texts", encoder)
    factory = isolated_environment["session_factory"]
    content = case.path.read_bytes()

    with factory() as db:
        paper = create_or_resume_paper(
            db,
            filename=case.path.name,
            content=content,
            content_type="application/pdf",
        )
        paper_id = int(paper.id)
    with factory() as db:
        embedding_service.index_paper(db, paper_id)
    initial = _database_state(factory, paper_id)
    _assert_chunk_order(initial, case.expected_pages)
    initial_chunk_count = len(initial["chunks"])
    initial_page_two_ids = {
        chunk["id"]
        for chunk in initial["chunks"]
        if chunk["page_number"] == 2
    }
    page_three_ids = {
        chunk["id"]
        for chunk in initial["chunks"]
        if chunk["page_number"] == 3
    }

    with factory() as db:
        page = (
            db.query(PaperPage)
            .filter(
                PaperPage.paper_id == paper_id,
                PaperPage.page_number == 2,
            )
            .one()
        )
        page.checkpoint_status = "failed"
        page.checkpoint_error = "real PDF regression: injected middle-page retry"
        db.commit()

    parsed_pages: list[int] = []
    with factory() as db:
        create_or_resume_paper(
            db,
            filename=case.path.name,
            content=content,
            content_type="application/pdf",
            page_observer=parsed_pages.append,
        )
    assert parsed_pages == [2]

    retried = _database_state(factory, paper_id)
    _assert_chunk_order(retried, case.expected_pages)
    assert len(retried["chunks"]) == initial_chunk_count
    retried_page_two_ids = {
        chunk["id"]
        for chunk in retried["chunks"]
        if chunk["page_number"] == 2
    }
    retried_page_three_ids = {
        chunk["id"]
        for chunk in retried["chunks"]
        if chunk["page_number"] == 3
    }
    assert retried_page_two_ids
    assert retried_page_two_ids != initial_page_two_ids
    assert retried_page_three_ids == page_three_ids

    with factory() as db:
        before_reindex = embedding_service.audit_paper_index_consistency(
            db,
            paper_id,
            batch_size=8,
        )
    assert before_reindex["parse_incomplete_page_count"] == 0
    assert before_reindex["missing_vector_count"] > 0

    with factory() as db:
        embedding_service.index_paper(db, paper_id)
        after_reindex = embedding_service.audit_paper_index_consistency(
            db,
            paper_id,
            batch_size=8,
        )
        global_audit = embedding_service.audit_index_consistency(
            db,
            batch_size=8,
        )
    _assert_clean_audit(after_reindex)
    _assert_clean_audit(global_audit)
