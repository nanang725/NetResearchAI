
"""
NetResearch AI - BGE-M3 向量检索服务

主要功能：
1. 延迟加载 BGE-M3 模型，避免启动 FastAPI 时立即占用大量内存
2. 使用 BGE-M3 将论文文本转换为 1024 维向量
3. 使用 ChromaDB 持久化保存向量
4. 支持单篇论文和全库向量检索
5. 支持为已有论文批量建立索引
6. 支持新论文建立索引和删除论文索引

注意：
- 不在模块导入时下载模型
- 不修改现有 SQLite 数据表结构
- ChromaDB 使用独立目录保存向量数据
"""

from __future__ import annotations

import difflib
import hashlib
import json
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable

import chromadb
from sentence_transformers import SentenceTransformer
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT, get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk, PaperPage
from app.services.paper_operation_coordinator import paper_operation_coordinator
from app.services.paper_service import (
    LEGACY_COMPLETE_PARSE_STATUSES,
    PDF_CHECKPOINT_SCHEMA_VERSION,
    get_paper_parse_completeness,
)


# =========================
# 路径和模型配置
# =========================

MODEL_NAME = "BAAI/bge-m3"

# 模型缓存目录
MODEL_DIR = BACKEND_ROOT / "data" / "models"

# ChromaDB 持久化目录
# ChromaDB 集合名称
COLLECTION_NAME = "netresearch_paper_chunks"

# 每次批量编码的文本数量
EMBEDDING_BATCH_SIZE = 16

# 每次写入 ChromaDB 的最大文本块数量
INDEX_BATCH_SIZE = 100

# Chroma metadata schema version. Increment when retrieval-valid metadata
# semantics change so old vectors are upgraded through normal incremental writes.
INDEX_SCHEMA_VERSION = 3
CONTENT_HASH_ALGORITHM = "sha256"
CONTENT_NORMALIZATION_VERSION = "nfkc-lf-strip-v1"

_INDEX_KEY_METADATA_FIELDS = (
    "chunk_id",
    "paper_id",
    "paper_title",
    "page_number",
    "page_chunk_index",
    "content_type",
    "element_id",
)

_MODEL_CACHE_REPOSITORY = "models--BAAI--bge-m3"
_MODEL_WEIGHT_PATTERNS = (
    "pytorch_model.bin",
    "model.safetensors",
    "pytorch_model-*.bin",
    "model-*.safetensors",
)


# =========================
# 全局资源：延迟初始化
# =========================

_model: SentenceTransformer | None = None
_chroma_client = None
_collection = None

_model_lock = threading.Lock()
_chroma_lock = threading.Lock()
_indexing_semaphore = threading.Semaphore(1)


def _is_complete_model_directory(path: Path) -> bool:
    if not path.is_dir() or not (path / "modules.json").is_file():
        return False

    return any(
        weight_path.is_file()
        for pattern in _MODEL_WEIGHT_PATTERNS
        for weight_path in path.rglob(pattern)
    )


def _find_local_model_path() -> Path | None:
    legacy_model_path = MODEL_DIR / "BAAI" / "bge-m3"
    if _is_complete_model_directory(legacy_model_path):
        return legacy_model_path

    cache_repository = MODEL_DIR / _MODEL_CACHE_REPOSITORY
    main_ref_path = cache_repository / "refs" / "main"
    if not main_ref_path.is_file():
        return None

    try:
        revision = main_ref_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None

    if not revision or Path(revision).name != revision:
        return None

    snapshot_path = cache_repository / "snapshots" / revision
    if _is_complete_model_directory(snapshot_path):
        return snapshot_path

    return None


def get_embedding_model() -> SentenceTransformer:
    """
    获取本地 BGE-M3 模型。

    只加载完整的本地模型，不回退到远程模型标识。
    """
    global _model

    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        local_model_path = _find_local_model_path()

        print("\n[BGE-M3] 正在加载向量模型...")

        if local_model_path is None:
            raise RuntimeError(
                "未找到完整的本地 BGE-M3 模型。已检查旧式目录和 "
                "Hugging Face 缓存快照；为避免联网，未尝试下载模型。"
            )

        print(f"[BGE-M3] 使用本地模型：{local_model_path}")

        try:
            _model = SentenceTransformer(
                str(local_model_path),
                cache_folder=str(MODEL_DIR),
                device=None,
                local_files_only=True,
            )
        except Exception as exc:
            _model = None
            raise RuntimeError(
                "本地 BGE-M3 模型加载失败，快照可能不完整。"
                f" model_path={local_model_path}; 未尝试下载。原始错误：{exc}"
            ) from exc

        print("[BGE-M3] 模型加载成功。")

        return _model


def get_chroma_dir() -> Path:
    """Return the configured persistent Chroma directory."""
    return get_settings().chroma_path


def get_chroma_collection():
    """
    获取 ChromaDB 持久化集合。

    数据保存在：
    CHROMA_DIR, defaulting to backend/data/chroma_db
    """
    global _chroma_client, _collection

    if _collection is not None:
        return _collection

    with _chroma_lock:
        if _collection is not None:
            return _collection

        chroma_dir = get_chroma_dir()
        chroma_dir.mkdir(parents=True, exist_ok=True)

        print("[ChromaDB] 正在初始化本地向量数据库...")

        _chroma_client = chromadb.PersistentClient(
            path=str(chroma_dir)
        )

        _collection = _chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={
                "description": "NetResearch AI academic paper chunks",
                "hnsw:space": "cosine",
            },
        )

        print(
            f"[ChromaDB] 初始化完成，"
            f"当前向量数量：{_collection.count()}"
        )

        return _collection


# =========================
# 文本向量化
# =========================

def encode_texts(texts: list[str]) -> list[list[float]]:
    """
    将多个文本转换为 BGE-M3 向量。

    normalize_embeddings=True：
    对向量进行归一化，方便余弦相似度检索。
    """
    if not texts:
        return []

    model = get_embedding_model()

    vectors = model.encode(
        texts,
        batch_size=EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )

    return vectors.tolist()


def encode_query(query: str) -> list[float]:
    """将用户问题转换为向量。"""
    query = (query or "").strip()

    if not query:
        raise ValueError("向量检索问题不能为空。")

    return encode_texts([query])[0]


def normalize_chunk_content(text: str) -> str:
    """Return the canonical form used for content fingerprints.

    Unicode compatibility normalization and line-ending normalization remove
    storage/export noise without collapsing meaningful spacing inside a chunk.
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    return normalized.replace("\r\n", "\n").replace("\r", "\n").strip()


def compute_chunk_content_hash(text: str) -> str:
    canonical = normalize_chunk_content(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_chunk_index_metadata(chunk: PaperChunk, paper: Paper) -> dict[str, Any]:
    """Build retriever-visible metadata and a deterministic chunk fingerprint."""
    content_hash = compute_chunk_content_hash(chunk.text)
    key_metadata = {
        "chunk_id": int(chunk.id),
        "paper_id": int(paper.id),
        "paper_title": " ".join(str(paper.title or "").split())[:500],
        "page_number": int(chunk.page_number),
        "page_chunk_index": int(chunk.page_chunk_index),
        "content_type": str(chunk.content_type or "paragraph").strip().lower(),
        "element_id": int(chunk.element_id) if chunk.element_id is not None else None,
    }
    return {
        **key_metadata,
        "content_hash": content_hash,
        "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
        "content_normalization_version": CONTENT_NORMALIZATION_VERSION,
        "index_fingerprint": compute_chunk_index_fingerprint(content_hash, key_metadata),
        "index_schema_version": INDEX_SCHEMA_VERSION,
    }


def compute_chunk_index_fingerprint(
    content_hash: str,
    key_metadata: dict[str, Any],
) -> str:
    payload = {
        "content_hash": content_hash,
        "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
        "content_normalization_version": CONTENT_NORMALIZATION_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "metadata": {
            field: key_metadata.get(field)
            for field in _INDEX_KEY_METADATA_FIELDS
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_chunk_index_record(
    chunk: PaperChunk,
    paper: Paper,
) -> tuple[str, dict[str, Any]]:
    """Return the Chroma document and metadata for an authoritative chunk."""
    document = chunk.text.strip()
    metadata = build_chunk_index_metadata(chunk, paper)
    return document, {
        key: value
        for key, value in metadata.items()
        if value is not None
    }


def index_record_mismatch_reasons(
    expected_document: str,
    expected_metadata: dict[str, Any],
    actual_document: str | None,
    actual_metadata: dict[str, Any] | None,
) -> list[str]:
    """Explain why an existing vector no longer represents its SQLite chunk."""
    actual_metadata = actual_metadata or {}
    reasons: list[str] = []

    required_metadata = (
        "content_hash",
        "content_hash_algorithm",
        "content_normalization_version",
        "index_fingerprint",
        "index_schema_version",
    )
    missing_metadata = [
        field for field in required_metadata if field not in actual_metadata
    ]
    if missing_metadata:
        reasons.append("legacy_index_missing_" + ",".join(missing_metadata))

    if (
        "index_schema_version" in actual_metadata
        and actual_metadata.get("index_schema_version") != INDEX_SCHEMA_VERSION
    ):
        reasons.append("index_schema_version_mismatch")
    if (
        "content_hash_algorithm" in actual_metadata
        and actual_metadata.get("content_hash_algorithm") != CONTENT_HASH_ALGORITHM
    ):
        reasons.append("content_hash_algorithm_mismatch")
    if (
        "content_normalization_version" in actual_metadata
        and actual_metadata.get("content_normalization_version")
        != CONTENT_NORMALIZATION_VERSION
    ):
        reasons.append("content_normalization_version_mismatch")

    expected_content_hash = expected_metadata["content_hash"]
    actual_content_hash = compute_chunk_content_hash(actual_document or "")
    if actual_content_hash != expected_content_hash:
        reasons.append("document_content_changed")
    elif (
        "content_hash" in actual_metadata
        and actual_metadata.get("content_hash") != actual_content_hash
    ):
        reasons.append("stored_content_hash_mismatch")

    for field in _INDEX_KEY_METADATA_FIELDS:
        expected_value = expected_metadata.get(field)
        actual_value = actual_metadata.get(field)
        if actual_value != expected_value:
            reasons.append(f"metadata_changed:{field}")

    if (
        "index_fingerprint" in actual_metadata
        and actual_metadata.get("index_fingerprint")
        != expected_metadata["index_fingerprint"]
    ):
        reasons.append("index_fingerprint_mismatch")

    return reasons


# =========================
# 单篇论文索引
# =========================


def index_paper(
    db: Session,
    paper_id: int,
    force: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    with paper_operation_coordinator.indexing(paper_id):
        with paper_operation_coordinator.collection_mutation():
            with _indexing_semaphore:
                return _index_paper(
                    db=db,
                    paper_id=paper_id,
                    force=force,
                    progress_callback=progress_callback,
                )


def _index_paper(
    db: Session,
    paper_id: int,
    force: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """
    为指定论文建立 BGE-M3 向量索引。

    处理原则：
    1. 索引前备份该论文已有的向量。
    2. 分批生成并写入向量。
    3. 全部成功后清理已经失效的旧向量。
    4. 中途失败时，删除本次可能产生的部分数据，
       并尝试恢复索引前的向量。
    """

    paper = (
        db.query(Paper)
        .filter(Paper.id == paper_id)
        .first()
    )

    if paper is None:
        raise ValueError(f"论文不存在：paper_id={paper_id}")

    completeness = get_paper_parse_completeness(db, paper_id)
    if not completeness.complete:
        paper.index_status = "failed"
        db.commit()
        raise AppError(
            completeness.failure_message(),
            code="PDF_PARSE_INCOMPLETE",
            status_code=409,
            details={
                "paper_id": paper_id,
                "incomplete_page_numbers": list(
                    completeness.incomplete_page_numbers
                ),
                "failed_page_count": completeness.failed_page_count,
                "processing_page_count": completeness.processing_page_count,
                "pending_page_count": completeness.pending_page_count,
                "missing_page_count": completeness.missing_page_count,
            },
        )

    chunks_query = (
        db.query(PaperChunk)
        .filter(PaperChunk.paper_id == paper_id)
        .order_by(
            PaperChunk.page_number.asc(),
            PaperChunk.page_chunk_index.asc(),
            PaperChunk.chunk_index.asc(),
            PaperChunk.id.asc(),
        )
    )
    chunk_count = chunks_query.count()

    if not chunk_count:
        paper.index_status = "failed"
        db.commit()

        return {
            "paper_id": paper_id,
            "indexed_count": 0,
            "status": "failed",
            "message": "该论文没有可用于向量索引的文本块。",
        }

    collection = get_chroma_collection()

    # 备份当前论文已有的向量。
    # 注意：这里不删除旧向量，避免重建失败后丢失原有数据。
    old_vectors = collection.get(
        where={"paper_id": paper_id},
        include=["documents", "metadatas", "embeddings"],
    )

    old_ids = old_vectors.get("ids", [])
    old_records = {
        vector_id: (document, metadata or {})
        for vector_id, document, metadata in zip(
            old_ids,
            old_vectors.get("documents", []),
            old_vectors.get("metadatas", []),
        )
    }

    paper.index_status = "indexing"
    db.commit()

    try:
        total_indexed = 0
        current_ids = set()

        for start in range(0, chunk_count, INDEX_BATCH_SIZE):
            batch = chunks_query.offset(start).limit(INDEX_BATCH_SIZE).all()
            valid_items = [
                (chunk, chunk.text.strip())
                for chunk in batch
                if chunk.text and chunk.text.strip()
            ]

            if not valid_items:
                continue

            batch_records = []
            for chunk, text in valid_items:
                vector_id = f"chunk_{chunk.id}"
                current_ids.add(vector_id)
                previous = old_records.get(vector_id)
                document, metadata = build_chunk_index_record(chunk, paper)
                unchanged = (
                    not force
                    and previous is not None
                    and not index_record_mismatch_reasons(
                        expected_document=document,
                        expected_metadata=metadata,
                        actual_document=previous[0],
                        actual_metadata=previous[1],
                    )
                )
                if not unchanged:
                    batch_records.append((vector_id, chunk, document, metadata))

            if batch_records:
                ids = [record[0] for record in batch_records]
                valid_chunks = [record[1] for record in batch_records]
                valid_texts = [record[2] for record in batch_records]
                metadatas = [record[3] for record in batch_records]
                embeddings = encode_texts(valid_texts)
                collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    documents=valid_texts,
                    metadatas=metadatas,
                )

            total_indexed += len(valid_items)
            if progress_callback is not None:
                progress_callback(total_indexed, chunk_count)

            if batch_records:
                print(
                    f"[向量索引] 论文 {paper_id}："
                    f"{total_indexed}/{chunk_count}"
                )

        if total_indexed == 0:
            raise ValueError("论文没有有效文本，无法生成向量。")

        # 所有新向量写入成功后，再清理已经不存在的旧文本块。
        obsolete_ids = [
            vector_id
            for vector_id in old_ids
            if vector_id not in current_ids
        ]

        if obsolete_ids:
            collection.delete(ids=obsolete_ids)

        paper.index_status = "indexed"
        db.commit()

        print(
            f"[向量索引] 论文 {paper_id} 索引完成，"
            f"共写入 {total_indexed} 条向量。"
        )

        return {
            "paper_id": paper_id,
            "indexed_count": total_indexed,
            "status": "indexed",
            "message": "向量索引建立完成。",
        }

    except Exception as exc:
        print(
            f"[向量索引] 论文 {paper_id} 索引失败：{exc}"
        )

        # ChromaDB 不受 SQLite 事务控制。
        # 因此需要单独恢复索引前的向量数据。
        try:
            collection.delete(
                where={"paper_id": paper_id}
            )

            if old_ids:
                old_embeddings = old_vectors.get("embeddings")

                if hasattr(old_embeddings, "tolist"):
                    old_embeddings = old_embeddings.tolist()

                collection.upsert(
                    ids=old_ids,
                    embeddings=old_embeddings,
                    documents=old_vectors.get("documents"),
                    metadatas=old_vectors.get("metadatas"),
                )

            print(
                f"[向量索引] 论文 {paper_id} "
                "原有向量数据恢复完成。"
            )

        except Exception as restore_exc:
            print(
                f"[向量索引] 警告：论文 {paper_id} "
                f"原有向量恢复失败：{restore_exc}"
            )

        db.rollback()

        paper = (
            db.query(Paper)
            .filter(Paper.id == paper_id)
            .first()
        )

        if paper is not None:
            paper.index_status = "failed"
            db.commit()

        raise



# =========================
# 全库论文索引
# =========================


def index_all_papers(
    db: Session,
    force: bool = False,
) -> dict[str, Any]:
    """
    为数据库中的所有论文逐篇建立向量索引。

    采用逐篇安全重建策略：
    1. 不清空整个 ChromaDB 集合。
    2. 每篇论文单独执行 index_paper。
    3. 单篇失败时恢复该论文原有向量。
    4. 单篇失败不影响其他论文继续索引。

    force 参数保留，用于兼容原有调用。
    当前 index_paper 会重新处理论文文本块，
    使用 upsert 更新已有向量，不会重复插入。
    """
    papers = (
        db.query(Paper)
        .order_by(Paper.id.asc())
        .all()
    )

    results = []

    for paper in papers:
        paper_id = paper.id

        try:
            result = index_paper(
                db=db,
                paper_id=paper_id,
                force=force,
            )

            results.append(result)

        except Exception as exc:
            results.append(
                {
                    "paper_id": paper_id,
                    "indexed_count": 0,
                    "status": "failed",
                    "message": str(exc),
                }
            )

            print(
                f"[向量索引失败] paper_id={paper_id}: {exc}"
            )

    success_count = sum(
        1
        for result in results
        if result["status"] == "indexed"
    )

    failed_count = len(papers) - success_count

    return {
        "total_papers": len(papers),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }



# =========================
# 向量检索
# =========================

def search_similar_chunks(
    query: str,
    limit: int = 5,
    paper_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    根据语义相似度检索论文文本块。

    参数：
        query：用户问题
        limit：返回数量
        paper_id：指定论文 ID；None 表示全库检索

    返回结构与 BM25 检索结果保持一致，
    方便后续进行 RRF 融合。
    """
    query = (query or "").strip()

    if not query:
        return []

    collection = get_chroma_collection()

    if collection.count() == 0:
        print("[向量检索] 当前向量数据库为空。")
        return []

    query_vector = encode_query(query)

    # 单篇模式只查询指定论文
    where = (
        {"paper_id": int(paper_id)}
        if paper_id is not None
        else None
    )

    # 先检查指定范围内是否有向量
    if where is not None:
        available = collection.get(
            where=where,
            include=[],
        )

        if not available["ids"]:
            return []

        n_results = min(
            limit,
            len(available["ids"]),
        )
    else:
        n_results = min(
            limit,
            collection.count(),
        )

    results = collection.query(
        query_embeddings=[query_vector],
        n_results=n_results,
        where=where,
        include=[
            "documents",
            "metadatas",
            "distances",
        ],
    )

    output: list[dict[str, Any]] = []

    ids = results.get("ids", [[]])[0]
    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for vector_id, text, metadata, distance in zip(
        ids,
        documents,
        metadatas,
        distances,
    ):
        if not metadata or not text:
            continue

        # ChromaDB cosine distance 越小越相似
        similarity = 1.0 - float(distance)

        output.append(
            {
                "chunk_id": int(metadata["chunk_id"]),
                "paper_id": int(metadata["paper_id"]),
                "paper_title": metadata["paper_title"],
                "page_number": int(metadata["page_number"]),
                "page_chunk_index": int(metadata.get("page_chunk_index", 0)),
                "text": text,
                "content_type": metadata.get("content_type", "paragraph"),
                "element_id": metadata.get("element_id"),
                "retrieval_method": "vector",
                "retrieval_score": round(similarity, 6),
                "vector_id": vector_id,
            }
        )

    print(
        f"[向量检索] query={query!r}, "
        f"paper_id={paper_id}, "
        f"返回结果={len(output)}"
    )

    return output


def find_similar_papers(
    db: Session,
    paper_id: int,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return explainable BGE-M3 candidates; this never links or deletes papers."""
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if paper is None:
        raise ValueError(f"论文不存在：paper_id={paper_id}")

    output_by_id = {
        candidate["paper_id"]: candidate
        for candidate in _find_metadata_candidates(db, paper)
    }
    sample_pages = (
        db.query(PaperChunk)
        .filter(PaperChunk.paper_id == paper_id)
        .order_by(
            PaperChunk.page_number.asc(),
            PaperChunk.page_chunk_index.asc(),
            PaperChunk.chunk_index.asc(),
            PaperChunk.id.asc(),
        )
        .limit(6)
        .all()
    )
    sample_text = "\n".join(chunk.text for chunk in sample_pages)[:3000]
    metadata = json.loads(paper.pdf_metadata) if paper.pdf_metadata else {}
    query_text = " ".join(
        part
        for part in (
            paper.title,
            paper.authors or "",
            str(metadata.get("subject") or ""),
            str(metadata.get("keywords") or ""),
            sample_text,
        )
        if part
    ).strip()
    if not query_text:
        return list(output_by_id.values())

    collection = get_chroma_collection()
    collection_count = collection.count()
    if collection_count == 0:
        return list(output_by_id.values())

    result_count = min(collection_count, max(limit * 10, 20))
    result = collection.query(
        query_embeddings=[encode_texts([query_text])[0]],
        n_results=result_count,
        include=["metadatas", "distances"],
    )
    best_by_paper: dict[int, float] = {}
    for metadata, distance in zip(
        result.get("metadatas", [[]])[0],
        result.get("distances", [[]])[0],
    ):
        if not metadata or metadata.get("paper_id") is None:
            continue
        candidate_id = int(metadata["paper_id"])
        if candidate_id == paper_id:
            continue
        similarity = 1.0 - float(distance)
        best_by_paper[candidate_id] = max(
            similarity,
            best_by_paper.get(candidate_id, -1.0),
        )

    candidate_ids = list(best_by_paper)
    if not candidate_ids:
        return []
    candidates = (
        db.query(Paper)
        .filter(Paper.id.in_(candidate_ids))
        .all()
    )
    threshold = get_settings().semantic_duplicate_threshold
    source_title = _normalize_similarity_text(paper.title)
    for candidate in candidates:
        semantic_score = best_by_paper[candidate.id]
        title_score = difflib.SequenceMatcher(
            None,
            source_title,
            _normalize_similarity_text(candidate.title),
        ).ratio()
        if semantic_score < threshold:
            continue
        candidate_result = output_by_id.get(candidate.id)
        semantic_reason = (
            "BGE-M3 语义相似候选；相似度为 "
            f"{semantic_score:.1%}，标题相似度为 {title_score:.1%}。"
            "该结果仅供人工核对，不会自动合并或删除。"
        )
        if candidate_result is None:
            output_by_id[candidate.id] = {
                "paper_id": candidate.id,
                "title": candidate.title,
                "authors": candidate.authors,
                "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
                "semantic_similarity": round(semantic_score, 4),
                "title_similarity": round(title_score, 4),
                "match_reason": semantic_reason,
                "candidate_type": (
                    "suspected_version"
                    if title_score >= 0.75 or semantic_score >= 0.97
                    else "possible_duplicate"
                ),
            }
        else:
            candidate_result["semantic_similarity"] = round(semantic_score, 4)
            candidate_result["title_similarity"] = round(title_score, 4)
            candidate_result["match_reason"] += f" {semantic_reason}"
    return sorted(
        output_by_id.values(),
        key=lambda item: (item["semantic_similarity"], item["title_similarity"]),
        reverse=True,
    )[:limit]


def _find_metadata_candidates(db: Session, paper: Paper) -> list[dict[str, Any]]:
    conditions = []
    if paper.doi:
        conditions.append(Paper.doi == paper.doi)
    if paper.normalized_title:
        conditions.append(Paper.normalized_title == paper.normalized_title)
    if not conditions:
        return []

    candidates = (
        db.query(Paper)
        .filter(Paper.id != paper.id, or_(*conditions))
        .all()
    )
    source_authors = _normalize_similarity_text(paper.authors or "")
    output = []
    for candidate in candidates:
        doi_match = bool(paper.doi and candidate.doi == paper.doi)
        title_match = bool(
            paper.normalized_title
            and candidate.normalized_title == paper.normalized_title
        )
        authors_match = bool(
            source_authors
            and source_authors == _normalize_similarity_text(candidate.authors or "")
        )
        year_match = bool(
            paper.publication_year
            and candidate.publication_year == paper.publication_year
        )
        venue_match = bool(
            paper.normalized_venue
            and candidate.normalized_venue == paper.normalized_venue
        )
        reasons = []
        if doi_match:
            reasons.append("DOI 一致")
        if title_match:
            reasons.append("规范化标题一致")
        if authors_match:
            reasons.append("作者一致")
        if year_match:
            reasons.append("发表年份一致")
        if venue_match:
            reasons.append("期刊/会议一致")
        title_score = difflib.SequenceMatcher(
            None,
            _normalize_similarity_text(paper.title),
            _normalize_similarity_text(candidate.title),
        ).ratio()
        output.append(
            {
                "paper_id": candidate.id,
                "title": candidate.title,
                "authors": candidate.authors,
                "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
                "semantic_similarity": 0.0,
                "title_similarity": round(title_score, 4),
                "metadata_similarity": 1.0 if doi_match else 0.95 if title_match else 0.0,
                "match_reason": (
                    "学术元数据候选（"
                    + "、".join(reasons)
                    + "）；仅供人工核对，不会自动合并或删除。"
                ),
                "candidate_type": (
                    "suspected_version"
                    if doi_match or (title_match and authors_match and year_match)
                    else "possible_duplicate"
                ),
            }
        )
    return output


def _empty_index_audit(scope: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "sqlite_chunk_count": 0,
        "chroma_vector_count": 0,
        "missing_vector_count": 0,
        "stale_vector_count": 0,
        "legacy_vector_count": 0,
        "orphan_vector_count": 0,
        "unverifiable_vector_count": 0,
        "read_error_count": 0,
        "parse_incomplete_page_count": 0,
        "parse_affected_paper_ids": [],
        "parse_issue_details": [],
        "missing_vector_ids": [],
        "stale_vector_ids": [],
        "legacy_vector_ids": [],
        "orphan_vector_ids": [],
        "affected_paper_ids": [],
        "orphan_paper_ids": [],
        "issues": [],
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
        "content_normalization_version": CONTENT_NORMALIZATION_VERSION,
        "consistent": True,
    }


def _iter_chunk_batches(
    db: Session,
    batch_size: int,
    paper_ids: list[int] | None,
):
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if paper_ids is not None and not paper_ids:
        return

    cursor = 0
    while True:
        query = (
            db.query(PaperChunk, Paper)
            .join(Paper, Paper.id == PaperChunk.paper_id)
            .filter(PaperChunk.id > cursor)
        )
        if paper_ids is not None:
            query = query.filter(PaperChunk.paper_id.in_(paper_ids))
        rows = (
            query
            .order_by(PaperChunk.id.asc())
            .limit(batch_size)
            .all()
        )
        if not rows:
            return
        yield rows
        cursor = rows[-1][0].id
        if len(rows) < batch_size:
            return


def _fetch_vector_records(collection, vector_ids: list[str]) -> dict[str, tuple[Any, Any]]:
    response = collection.get(
        ids=vector_ids,
        include=["documents", "metadatas"],
    )
    ids = response.get("ids", [])
    documents = response.get("documents", [])
    metadatas = response.get("metadatas", [])
    records = {}
    for index, vector_id in enumerate(ids):
        records[vector_id] = (
            documents[index] if index < len(documents) else None,
            metadatas[index] if index < len(metadatas) else None,
        )
    return records


def _add_audit_issue(
    audit: dict[str, Any],
    status: str,
    vector_id: str,
    paper_id: int | None,
    chunk_id: int | None,
    reasons: list[str],
    max_issues: int,
) -> None:
    if len(audit["issues"]) >= max_issues:
        return
    audit["issues"].append(
        {
            "status": status,
            "vector_id": vector_id,
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "reasons": reasons,
        }
    )


def _audit_expected_chunks(
    db: Session,
    collection,
    audit: dict[str, Any],
    batch_size: int,
    paper_ids: list[int] | None,
    max_issues: int,
) -> None:
    affected_papers = set(audit["affected_paper_ids"])
    for rows in _iter_chunk_batches(db, batch_size, paper_ids):
        expected = {}
        for chunk, paper in rows:
            vector_id = f"chunk_{chunk.id}"
            document, metadata = build_chunk_index_record(chunk, paper)
            expected[vector_id] = (chunk, paper, document, metadata)

        audit["sqlite_chunk_count"] += len(expected)
        try:
            actual_by_id = _fetch_vector_records(
                collection,
                list(expected),
            )
        except Exception as exc:
            audit["read_error_count"] += 1
            audit["unverifiable_vector_count"] += len(expected)
            for vector_id, (chunk, paper, _document, _metadata) in expected.items():
                affected_papers.add(int(paper.id))
                _add_audit_issue(
                    audit,
                    "unverifiable",
                    vector_id,
                    int(paper.id),
                    int(chunk.id),
                    [f"chroma_read_failed:{type(exc).__name__}"],
                    max_issues,
                )
            continue

        for vector_id, (chunk, paper, document, metadata) in expected.items():
            actual = actual_by_id.get(vector_id)
            if actual is None:
                audit["missing_vector_count"] += 1
                if len(audit["missing_vector_ids"]) < 50:
                    audit["missing_vector_ids"].append(vector_id)
                affected_papers.add(int(paper.id))
                _add_audit_issue(
                    audit,
                    "missing",
                    vector_id,
                    int(paper.id),
                    int(chunk.id),
                    [],
                    max_issues,
                )
                continue

            actual_document, actual_metadata = actual
            reasons = index_record_mismatch_reasons(
                expected_document=document,
                expected_metadata=metadata,
                actual_document=actual_document,
                actual_metadata=actual_metadata,
            )
            if not reasons:
                continue

            audit["stale_vector_count"] += 1
            if len(audit["stale_vector_ids"]) < 50:
                audit["stale_vector_ids"].append(vector_id)
            if any(reason.startswith("legacy_index_") for reason in reasons):
                audit["legacy_vector_count"] += 1
                if len(audit["legacy_vector_ids"]) < 50:
                    audit["legacy_vector_ids"].append(vector_id)
            affected_papers.add(int(paper.id))
            _add_audit_issue(
                audit,
                "stale",
                vector_id,
                int(paper.id),
                int(chunk.id),
                reasons,
                max_issues,
            )

    audit["affected_paper_ids"] = sorted(affected_papers)


def _parse_vector_chunk_id(vector_id: str) -> int | None:
    prefix = "chunk_"
    if not vector_id.startswith(prefix):
        return None
    value = vector_id[len(prefix):]
    if not value.isdigit():
        return None
    return int(value)


def _audit_orphaned_vectors(
    db: Session,
    collection,
    audit: dict[str, Any],
    batch_size: int,
    max_issues: int,
) -> None:
    total = int(collection.count())
    audit["chroma_vector_count"] = total
    orphan_papers = set(audit["orphan_paper_ids"])
    offset = 0
    while offset < total:
        page_size = min(batch_size, total - offset)
        try:
            page = collection.get(
                limit=page_size,
                offset=offset,
                include=["metadatas"],
            )
        except Exception as exc:
            audit["read_error_count"] += 1
            audit["unverifiable_vector_count"] += page_size
            offset += page_size
            _add_audit_issue(
                audit,
                "unverifiable",
                f"chroma_page_offset_{offset - page_size}",
                None,
                None,
                [f"chroma_read_failed:{type(exc).__name__}"],
                max_issues,
            )
            continue

        ids = list(page.get("ids", []))
        if not ids:
            remaining = total - offset
            if remaining:
                audit["read_error_count"] += 1
                audit["unverifiable_vector_count"] += remaining
                _add_audit_issue(
                    audit,
                    "unverifiable",
                    f"chroma_page_offset_{offset}",
                    None,
                    None,
                    ["chroma_page_returned_no_ids"],
                    max_issues,
                )
            break
        metadatas = page.get("metadatas", [])
        chunk_ids = {
            chunk_id
            for vector_id in ids
            if (chunk_id := _parse_vector_chunk_id(vector_id)) is not None
        }
        existing_chunk_ids = {
            int(chunk_id)
            for (chunk_id,) in (
                db.query(PaperChunk.id)
                .filter(PaperChunk.id.in_(chunk_ids))
                .all()
                if chunk_ids
                else []
            )
        }

        for index, vector_id in enumerate(ids):
            chunk_id = _parse_vector_chunk_id(vector_id)
            if chunk_id is not None and chunk_id in existing_chunk_ids:
                continue
            metadata = metadatas[index] if index < len(metadatas) else {}
            paper_id = None
            if metadata and metadata.get("paper_id") is not None:
                try:
                    paper_id = int(metadata["paper_id"])
                except (TypeError, ValueError):
                    paper_id = None
            if paper_id is not None:
                orphan_papers.add(paper_id)
            audit["orphan_vector_count"] += 1
            if len(audit["orphan_vector_ids"]) < 50:
                audit["orphan_vector_ids"].append(vector_id)
            _add_audit_issue(
                audit,
                "orphaned",
                vector_id,
                paper_id,
                chunk_id,
                [],
                max_issues,
            )
        offset += len(ids)

    audit["orphan_paper_ids"] = sorted(orphan_papers)


def _finalize_index_audit(audit: dict[str, Any]) -> dict[str, Any]:
    audit["consistent"] = not (
        audit["missing_vector_count"]
        or audit["stale_vector_count"]
        or audit["orphan_vector_count"]
        or audit["unverifiable_vector_count"]
        or audit["parse_incomplete_page_count"]
    )
    return audit


def _audit_orphans_only(
    db: Session,
    collection,
    batch_size: int,
    max_issues: int = 100,
) -> dict[str, Any]:
    audit = _empty_index_audit("collection")
    _audit_orphaned_vectors(
        db=db,
        collection=collection,
        audit=audit,
        batch_size=batch_size,
        max_issues=max_issues,
    )
    return _finalize_index_audit(audit)


def _audit_parse_completeness(
    db: Session,
    audit: dict[str, Any],
    paper_ids: list[int] | None,
) -> None:
    paper_query = db.query(
        Paper.id,
        Paper.parse_source_page_count,
        Paper.page_count,
        Paper.parse_status,
        Paper.parse_checkpoint_version,
    )
    if paper_ids is not None:
        if not paper_ids:
            return
        paper_query = paper_query.filter(Paper.id.in_(paper_ids))
    papers = paper_query.all()
    if not papers:
        return

    page_status_query = (
        db.query(
            PaperPage.paper_id,
            PaperPage.checkpoint_status,
            func.count(PaperPage.id),
        )
        .filter(
            PaperPage.paper_id.in_([int(paper.id) for paper in papers])
        )
        .group_by(PaperPage.paper_id, PaperPage.checkpoint_status)
    )
    status_counts: dict[int, dict[str, int]] = {}
    for paper_id, status, count in page_status_query.all():
        status_counts.setdefault(int(paper_id), {})[status] = int(count)

    affected_paper_ids = []
    incomplete_total = 0
    details = []
    for paper_id, source_page_count, page_count, parse_status, schema_version in papers:
        expected = int(source_page_count or page_count or 0)
        counts = status_counts.get(int(paper_id), {})
        recorded = sum(counts.values())
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        processing = counts.get("processing", 0)
        pending = counts.get("pending", 0) + sum(
            count
            for status, count in counts.items()
            if status not in {"completed", "failed", "processing", "pending"}
        )
        missing = max(expected - recorded, 0)
        legacy_complete = (
            schema_version != PDF_CHECKPOINT_SCHEMA_VERSION
            and parse_status in LEGACY_COMPLETE_PARSE_STATUSES
            and expected > 0
            and recorded == expected
        )
        incomplete = 0 if legacy_complete else max(expected - completed, 0)
        if recorded > expected:
            incomplete += recorded - expected
        if incomplete <= 0:
            continue
        affected_paper_ids.append(int(paper_id))
        incomplete_total += incomplete
        if len(details) < 100:
            details.append(
                {
                    "paper_id": int(paper_id),
                    "expected_page_count": expected,
                    "recorded_page_count": recorded,
                    "completed_page_count": completed,
                    "failed_page_count": failed,
                    "processing_page_count": processing,
                    "pending_page_count": pending,
                    "missing_page_count": missing,
                    "incomplete_page_count": incomplete,
                }
            )

    audit["parse_incomplete_page_count"] = incomplete_total
    audit["parse_affected_paper_ids"] = sorted(affected_paper_ids)
    audit["parse_issue_details"] = details


def audit_index_consistency(
    db: Session,
    batch_size: int = 5000,
    paper_ids: list[int] | None = None,
    max_issues: int = 100,
) -> dict[str, Any]:
    """Compare IDs, content fingerprints, metadata and schema without mutation."""
    collection = get_chroma_collection()
    audit = _empty_index_audit("papers" if paper_ids is not None else "collection")
    _audit_parse_completeness(db, audit, paper_ids)
    _audit_expected_chunks(
        db=db,
        collection=collection,
        audit=audit,
        batch_size=batch_size,
        paper_ids=paper_ids,
        max_issues=max_issues,
    )
    if paper_ids is None:
        _audit_orphaned_vectors(
            db=db,
            collection=collection,
            audit=audit,
            batch_size=batch_size,
            max_issues=max_issues,
        )
    return _finalize_index_audit(audit)


def audit_paper_index_consistency(
    db: Session,
    paper_id: int,
    batch_size: int = 5000,
    max_issues: int = 100,
) -> dict[str, Any]:
    return audit_index_consistency(
        db=db,
        batch_size=batch_size,
        paper_ids=[int(paper_id)],
        max_issues=max_issues,
    )


def _delete_orphaned_index_vectors(
    db: Session,
    collection,
    batch_size: int,
) -> dict[str, int]:
    total = int(collection.count())
    offset = 0
    deleted = 0
    while offset < total:
        page_size = min(batch_size, total - offset)
        page = collection.get(
            limit=page_size,
            offset=offset,
            include=["metadatas"],
        )
        ids = list(page.get("ids", []))
        if not ids:
            raise RuntimeError(
                f"Chroma returned no IDs at offset {offset} while {total} vectors exist."
            )
        chunk_ids = {
            chunk_id
            for vector_id in ids
            if (chunk_id := _parse_vector_chunk_id(vector_id)) is not None
        }
        existing_chunk_ids = {
            int(chunk_id)
            for (chunk_id,) in (
                db.query(PaperChunk.id)
                .filter(PaperChunk.id.in_(chunk_ids))
                .all()
                if chunk_ids
                else []
            )
        }
        candidate_orphan_ids = [
            vector_id
            for vector_id in ids
            if (
                (chunk_id := _parse_vector_chunk_id(vector_id)) is None
                or chunk_id not in existing_chunk_ids
            )
        ]
        if candidate_orphan_ids:
            with paper_operation_coordinator.collection_mutation():
                candidate_chunk_ids = {
                    chunk_id
                    for vector_id in candidate_orphan_ids
                    if (chunk_id := _parse_vector_chunk_id(vector_id)) is not None
                }
                present_after_recheck = {
                    int(chunk_id)
                    for (chunk_id,) in (
                        db.query(PaperChunk.id)
                        .filter(PaperChunk.id.in_(candidate_chunk_ids))
                        .all()
                        if candidate_chunk_ids
                        else []
                    )
                }
                orphan_ids = [
                    vector_id
                    for vector_id in candidate_orphan_ids
                    if (
                        (chunk_id := _parse_vector_chunk_id(vector_id)) is None
                        or chunk_id not in present_after_recheck
                    )
                ]
                if orphan_ids:
                    collection.delete(ids=orphan_ids)
                    after_delete = collection.get(ids=orphan_ids, include=[])
                    remaining_ids = set(after_delete.get("ids", [])).intersection(
                        orphan_ids
                    )
                    if remaining_ids:
                        raise RuntimeError(
                            "Chroma orphan deletion was incomplete: "
                            f"{len(remaining_ids)} of {len(orphan_ids)} IDs remain."
                        )
                    deleted += len(orphan_ids)
            retained_count = len(ids) - len(orphan_ids)
            total = int(collection.count())
            offset += retained_count
        else:
            offset += len(ids)
    return {
        "orphaned_vector_count_removed": deleted,
        "remaining_collection_vector_count": int(collection.count()),
    }


def reconcile_index_consistency(
    db: Session,
    batch_size: int = 5000,
    paper_ids: list[int] | None = None,
    raise_on_inconsistent: bool = True,
) -> dict[str, Any]:
    """Remove confirmed orphans, then verify global or targeted chunk freshness."""
    collection = get_chroma_collection()
    cleanup = _delete_orphaned_index_vectors(db, collection, batch_size)
    if paper_ids is None:
        audit = audit_index_consistency(db, batch_size=batch_size)
    else:
        audit = audit_index_consistency(
            db,
            batch_size=batch_size,
            paper_ids=[int(paper_id) for paper_id in paper_ids],
        )
        orphan_audit = _audit_orphans_only(
            db=db,
            collection=collection,
            batch_size=batch_size,
        )
        audit["chroma_vector_count"] = orphan_audit["chroma_vector_count"]
        audit["orphan_vector_count"] = orphan_audit["orphan_vector_count"]
        audit["orphan_vector_ids"] = orphan_audit["orphan_vector_ids"]
        audit["orphan_paper_ids"] = orphan_audit["orphan_paper_ids"]
        audit["unverifiable_vector_count"] += orphan_audit[
            "unverifiable_vector_count"
        ]
        audit["read_error_count"] += orphan_audit["read_error_count"]
        audit["issues"] = (
            audit["issues"] + orphan_audit["issues"]
        )[:100]
        _finalize_index_audit(audit)
    audit.update(cleanup)

    if not audit["consistent"]:
        if raise_on_inconsistent:
            raise RuntimeError(
                "Index reconciliation did not converge: "
                f"missing={audit['missing_vector_count']} "
                f"stale={audit['stale_vector_count']} "
                f"orphaned={audit['orphan_vector_count']} "
                f"unverifiable={audit['unverifiable_vector_count']} "
                f"parse_incomplete={audit['parse_incomplete_page_count']}."
            )
        return audit
    return audit


def _normalize_similarity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


# =========================
# 删除论文向量
# =========================

def delete_paper_vectors(paper_id: int) -> None:
    """
    删除指定论文的所有向量。

    应在删除论文时同步调用。
    """
    with paper_operation_coordinator.collection_mutation():
        collection = get_chroma_collection()

        collection.delete(
            where={
                "paper_id": int(paper_id)
            }
        )

    print(
        f"[向量索引] 已删除论文 {paper_id} 的向量。"
    )


# =========================
# 索引状态查询
# =========================

def get_vector_index_stats() -> dict[str, Any]:
    """获取当前 ChromaDB 向量索引统计信息。"""
    collection = get_chroma_collection()

    return {
        "collection_name": COLLECTION_NAME,
        "collection_count": collection.count(),
        "storage_path": str(get_chroma_dir()),
        "model_name": MODEL_NAME,
    }
