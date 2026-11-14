
"""
NetResearch AI - 论文管理与智能问答 API

功能：
1. PDF 上传与解析
2. 论文列表、详情和删除
3. BM25 + BGE-M3 混合检索
4. RRF 融合排序
5. 单篇论文问答
6. 全库论文问答
7. 新论文上传后自动建立向量索引
8. 删除论文时同步删除向量数据
9. 增加检索证据相关性判断，隐藏无关证据
"""

import logging

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.responses import success
from app.database import get_db
from app.models.paper import Paper

from app.services.paper_service import (
    create_paper,
    delete_paper,
    finish_paper_deletion,
    get_paper,
    list_papers,
    serialize_paper,
)

from app.services.llm_service import (
    generate_rag_answer,
    filter_relevant_contexts,
    is_enumeration_query,
)

from app.services.retrieval_service import (
    search_chunks_hybrid,
)

from app.services.embedding_service import (
    index_paper,
    delete_paper_vectors,
)


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/papers",
    tags=["papers"],
)


@router.post("/upload")
async def upload_paper(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    上传 PDF 论文、解析文本并自动建立向量索引。
    """

    if not file.filename:
        raise AppError(
            "请选择要上传的 PDF 文件。",
            code="EMPTY_FILENAME",
            status_code=400,
        )

    settings = get_settings()
    max_upload_bytes = settings.max_upload_bytes
    content_chunks: list[bytes] = []
    total_size = 0

    try:
        while True:
            remaining = max_upload_bytes - total_size
            chunk = await file.read(min(1024 * 1024, remaining + 1))

            if not chunk:
                break

            total_size += len(chunk)
            if total_size > max_upload_bytes:
                raise AppError(
                    f"文件大小超过 {settings.max_upload_mb}MB 限制。",
                    code="FILE_TOO_LARGE",
                    status_code=400,
                )
            content_chunks.append(chunk)
    finally:
        await file.close()

    content = b"".join(content_chunks)

    paper = create_paper(
        db=db,
        filename=file.filename,
        content=content,
        content_type=file.content_type or "application/pdf",
    )

    # PDF 上传和解析成功后，自动建立向量索引
    try:
        indexed = index_paper(
            db=db,
            paper_id=paper.id,
            force=False,
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Vector indexing failed for uploaded paper_id=%s", paper.id)
        _mark_index_failed(db, paper.id)
    else:
        if indexed.get("status") == "indexed":
            logger.info("Vector indexing completed for paper_id=%s", paper.id)
        else:
            logger.error(
                "Paper was saved but vector indexing did not complete: "
                "paper_id=%s status=%s reason=%s",
                paper.id,
                indexed.get("status"),
                indexed.get("message", "unknown"),
            )
            _mark_index_failed(db, paper.id)

    db.refresh(paper)

    return success(
        serialize_paper(
            paper,
            include_pages=True,
        ),
        status_code=201,
    )


def _mark_index_failed(db: Session, paper_id: int) -> None:
    try:
        db.rollback()
        paper = db.query(Paper).filter(Paper.id == paper_id).first()
        if paper is None:
            raise ValueError(f"Saved paper disappeared during indexing: {paper_id}")
        paper.index_status = "failed"
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Could not persist failed indexing state for paper_id=%s", paper_id)
        raise AppError(
            "论文已保存，但无法记录向量索引失败状态。",
            code="INDEX_STATUS_UPDATE_FAILED",
            status_code=500,
        ) from exc


@router.get("")
def list_all_papers(
    db: Session = Depends(get_db),
):
    """
    获取论文库列表。
    """

    papers = list_papers(db)

    return success(
        [
            serialize_paper(
                paper,
                include_pages=False,
            )
            for paper in papers
        ]
    )


@router.get("/search")
def search_papers(
    q: str = "",
    db: Session = Depends(get_db),
):
    """
    使用 BM25 + BGE-M3 + RRF 进行混合检索。
    """

    if not q.strip():
        return success([])

    results = search_chunks_hybrid(
        db=db,
        query=q,
        limit=10,
    )

    return success(results)


@router.post("/agent/chat")
def agent_chat(
    query: str,
    paper_id: int | None = None,
    db: Session = Depends(get_db),
):
    """
    论文智能问答。

    paper_id 有值：只检索指定论文。
    paper_id 为空：检索整个论文库。

    检索流程：
    BM25 + BGE-M3 -> RRF -> 证据相关性判断 -> DeepSeek
    """

    query = (query or "").strip()

    if not query:
        raise AppError(
            "问题不能为空。",
            code="EMPTY_QUERY",
            status_code=400,
        )

    print(
        f"\n=== 收到问答请求 ===\n"
        f"检索范围："
        f"{'单篇论文' if paper_id is not None else '全库'}\n"
        f"paper_id：{paper_id}\n"
        f"问题：{query}"
    )

    steps = [
        "🤖 已接收问题，准备检索文献。",
    ]

    # 检查单篇论文是否存在
    if paper_id is not None:

        paper = get_paper(
            db=db,
            paper_id=paper_id,
        )

        steps.append(
            f"🔍 检索范围：单篇论文《{paper.title}》"
        )

    else:
        steps.append(
            "🔍 检索范围：全库联合检索"
        )

    # 枚举型单篇问题需要更多候选证据；普通问答和全库问答保持原 Top-5。
    enumeration_query = (
        paper_id is not None
        and is_enumeration_query(query)
    )
    retrieval_limit = 8 if enumeration_query else 5

    # 执行 BM25 + BGE-M3 混合检索
    contexts = search_chunks_hybrid(
        db=db,
        query=query,
        limit=retrieval_limit,
        paper_id=paper_id,
    )

    # 统计检索来源
    bm25_count = sum(
        1
        for item in contexts
        if "bm25" in item.get("retrieval_sources", [])
    )

    vector_count = sum(
        1
        for item in contexts
        if "vector" in item.get("retrieval_sources", [])
    )

    hybrid_count = sum(
        1
        for item in contexts
        if item.get("retrieval_method") == "hybrid"
    )

    steps.append(
        f"🔎 BM25 关键词检索完成，"
        f"参与最终结果的证据：{bm25_count} 条。"
    )

    steps.append(
        f"🧠 BGE-M3 语义检索完成，"
        f"参与最终结果的证据：{vector_count} 条。"
    )

    steps.append(
        f"🔗 RRF 融合排序完成，"
        f"共找到 {len(contexts)} 条最终证据，"
        f"其中双路命中 {hybrid_count} 条。"
    )

    # 没有检索到证据时，不调用大模型
    if not contexts:

        steps.append(
            "⚠️ 没有找到匹配证据，暂不生成推测性回答。"
        )

        print(
            "=== 检索结束：没有找到证据 ===\n"
        )

        return success(
            {
                "answer": (
                    "根据当前知识库，未找到相关证据。"
                    "你可以尝试更换问题中的关键词，"
                    "或确认论文是否已经成功解析并建立索引。"
                ),
                "steps": steps,
                "contexts": [],
                "is_relevant": False,
            }
        )


    # 对检索结果进行逐条证据相关性过滤
    steps.append(
        "🔍 正在逐条检查检索证据与用户问题的相关性。"
    )

    filtered_contexts = filter_relevant_contexts(
        query=query,
        contexts=contexts,
    )

    # 记录过滤结果
    removed_count = len(contexts) - len(filtered_contexts)

    steps.append(
        f"🧹 证据过滤完成：原始 {len(contexts)} 条，"
        f"保留 {len(filtered_contexts)} 条，"
        f"排除 {removed_count} 条无关证据。"
    )

    # 过滤后没有相关证据，拒绝生成答案
    if not filtered_contexts:

        steps.append(
            "⚠️ 没有找到与当前问题实质相关的论文证据，"
            "已停止回答。"
        )

        print(
            "=== 问答结束：逐条过滤后没有相关证据 ===\n"
        )

        return success(
            {
                "answer": (
                    "抱歉，当前知识库中虽然检索到了一些论文内容，"
                    "但经过逐条相关性检查，没有找到能够支持回答"
                    "您当前问题的有效证据。"
                    "请尝试提出与已上传论文研究内容相关的问题。"
                ),
                "steps": steps,
                "contexts": [],
                "is_relevant": False,
            }
        )

    # 只使用过滤后的证据生成答案
    steps.append(
        "✅ 已确认存在相关论文证据，"
        "正在根据筛选后的论文原文生成回答。"
    )

    answer = generate_rag_answer(
        query=query,
        contexts=filtered_contexts,
    )

    steps.append(
        "✅ 回答生成完成。"
    )

    print(
        f"=== 问答完成：原始 {len(contexts)} 条证据，"
        f"过滤后返回 {len(filtered_contexts)} 条证据 ===\n"
    )

    return success(
        {
            "answer": answer,
            "steps": steps,
            "contexts": filtered_contexts,
            "is_relevant": True,
        }
    )


@router.get("/{paper_id}")
def get_paper_detail(
    paper_id: int,
    db: Session = Depends(get_db),
):
    """
    获取论文详情及逐页文本。
    """

    paper = get_paper(
        db=db,
        paper_id=paper_id,
        with_pages=True,
    )

    return success(
        serialize_paper(
            paper,
            include_pages=True,
        )
    )


@router.delete("/{paper_id}")
def remove_paper(
    paper_id: int,
    db: Session = Depends(get_db),
):
    deletion = delete_paper(db=db, paper_id=paper_id)
    cleanup_pending = finish_paper_deletion(
        deletion,
        delete_paper_vectors,
    )
    if cleanup_pending:
        logger.error(
            "Paper database deletion committed but external cleanup remains pending: "
            "paper_id=%s journal=%s",
            paper_id,
            deletion.journal_path,
        )
        raise AppError(
            "论文记录已删除，但部分外部资源清理失败，服务将在下次启动时重试。",
            code="PAPER_CLEANUP_PENDING",
            status_code=503,
            details={"paper_id": paper_id},
        )
    else:
        logger.info("Paper deletion completed: paper_id=%s", paper_id)

    return success(
        {
            "id": paper_id,
            "deleted": True,
        }
    )
