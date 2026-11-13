
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

import threading
from pathlib import Path
from typing import Any

import chromadb
from sentence_transformers import SentenceTransformer
from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT
from app.models.paper import Paper, PaperChunk


# =========================
# 路径和模型配置
# =========================

MODEL_NAME = "BAAI/bge-m3"

# 模型缓存目录
MODEL_DIR = BACKEND_ROOT / "data" / "models"

# ChromaDB 持久化目录
CHROMA_DIR = BACKEND_ROOT / "data" / "chroma_db"

# ChromaDB 集合名称
COLLECTION_NAME = "netresearch_paper_chunks"

# 每次批量编码的文本数量
EMBEDDING_BATCH_SIZE = 16

# 每次写入 ChromaDB 的最大文本块数量
INDEX_BATCH_SIZE = 100


# =========================
# 全局资源：延迟初始化
# =========================

_model: SentenceTransformer | None = None
_chroma_client = None
_collection = None

_model_lock = threading.Lock()
_chroma_lock = threading.Lock()


def get_embedding_model() -> SentenceTransformer:
    """
    获取 BGE-M3 模型。

    首次调用时加载模型。
    如果本地没有模型，则从 Hugging Face 下载。
    """
    global _model

    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        MODEL_DIR.mkdir(parents=True, exist_ok=True)

        local_model_path = MODEL_DIR / "BAAI" / "bge-m3"

        print("\n[BGE-M3] 正在加载向量模型...")

        if local_model_path.exists():
            model_source = str(local_model_path)
            print(f"[BGE-M3] 使用本地模型：{model_source}")
        else:
            model_source = MODEL_NAME
            print(f"[BGE-M3] 本地模型不存在，准备下载：{MODEL_NAME}")

        try:
            _model = SentenceTransformer(
                model_source,
                cache_folder=str(MODEL_DIR),
                device=None,
            )
        except Exception as exc:
            _model = None
            raise RuntimeError(
                "BGE-M3 模型加载失败。请检查网络连接、模型下载状态和依赖安装。"
                f" 原始错误：{exc}"
            ) from exc

        print("[BGE-M3] 模型加载成功。")

        return _model


def get_chroma_collection():
    """
    获取 ChromaDB 持久化集合。

    数据保存在：
    backend/data/chroma_db
    """
    global _chroma_client, _collection

    if _collection is not None:
        return _collection

    with _chroma_lock:
        if _collection is not None:
            return _collection

        CHROMA_DIR.mkdir(parents=True, exist_ok=True)

        print("[ChromaDB] 正在初始化本地向量数据库...")

        _chroma_client = chromadb.PersistentClient(
            path=str(CHROMA_DIR)
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


# =========================
# 单篇论文索引
# =========================


def index_paper(
    db: Session,
    paper_id: int,
    force: bool = False,
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

    chunks = (
        db.query(PaperChunk)
        .filter(PaperChunk.paper_id == paper_id)
        .order_by(PaperChunk.id.asc())
        .all()
    )

    if not chunks:
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

    paper.index_status = "indexing"
    db.commit()

    try:
        total_indexed = 0
        current_ids = set()

        for start in range(0, len(chunks), INDEX_BATCH_SIZE):
            batch = chunks[start:start + INDEX_BATCH_SIZE]

            valid_items = [
                (chunk, chunk.text.strip())
                for chunk in batch
                if chunk.text and chunk.text.strip()
            ]

            if not valid_items:
                continue

            valid_chunks = [item[0] for item in valid_items]
            valid_texts = [item[1] for item in valid_items]

            embeddings = encode_texts(valid_texts)

            ids = [
                f"chunk_{chunk.id}"
                for chunk in valid_chunks
            ]

            metadatas = [
                {
                    "chunk_id": int(chunk.id),
                    "paper_id": int(paper.id),
                    "paper_title": str(paper.title)[:500],
                    "page_number": int(chunk.page_number),
                }
                for chunk in valid_chunks
            ]

            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=valid_texts,
                metadatas=metadatas,
            )

            current_ids.update(ids)
            total_indexed += len(valid_chunks)

            print(
                f"[向量索引] 论文 {paper_id}："
                f"{total_indexed}/{len(chunks)}"
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
                "text": text,
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


# =========================
# 删除论文向量
# =========================

def delete_paper_vectors(paper_id: int) -> None:
    """
    删除指定论文的所有向量。

    应在删除论文时同步调用。
    """
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
        "storage_path": str(CHROMA_DIR),
        "model_name": MODEL_NAME,
    }
