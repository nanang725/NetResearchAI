
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
"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.responses import success
from app.database import get_db

from app.services.paper_service import (
    create_paper,
    delete_paper,
    get_paper,
    list_papers,
    serialize_paper,
)

from app.services.llm_service import generate_rag_answer

from app.services.retrieval_service import (
    search_chunks_hybrid,
)

from app.services.embedding_service import (
    index_paper,
    delete_paper_vectors,
)


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

    content = await file.read()

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


        if indexed.get("status") == "indexed":
            print(
                f"[论文上传] 论文 {paper.id} "
                "向量索引成功。"
            )
        else:
            print(
                f"[论文上传] 论文 {paper.id} 已保存，"
                f"但向量索引未成功。"
                f"状态：{indexed.get('status')}，"
                f"原因：{indexed.get('message', '未知原因')}"
            )


    except Exception as exc:
        db.rollback()

        print(
            f"[论文上传] 向量索引异常，"
            f"论文 {paper.id} 已保存：{exc}"
        )

    # 刷新论文状态
    db.refresh(paper)

    return success(
        serialize_paper(
            paper,
            include_pages=True,
        ),
        status_code=201,
    )


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
    BM25 + BGE-M3 -> RRF -> DeepSeek
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

    # 执行混合检索
    contexts = search_chunks_hybrid(
        db=db,
        query=query,
        limit=5,
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

    # 没有证据时不调用大模型
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
            }
        )

    # 将检索到的论文原文交给 DeepSeek
    steps.append(
        "🧠 正在根据检索到的论文原文生成回答。"
    )

    answer = generate_rag_answer(
        query=query,
        contexts=contexts,
    )

    steps.append(
        "✅ 回答生成完成。"
    )

    print(
        f"=== 问答完成："
        f"返回 {len(contexts)} 条证据 ===\n"
    )

    return success(
        {
            "answer": answer,
            "steps": steps,
            "contexts": contexts,
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
    """
    删除论文及其对应的向量数据。
    """

    # 先确认论文存在
    get_paper(
        db=db,
        paper_id=paper_id,
    )

    # 先删除 ChromaDB 中对应的向量
    delete_paper_vectors(paper_id)

    # 再删除 SQLite 中的论文及关联数据
    delete_paper(
        db=db,
        paper_id=paper_id,
    )

    print(
        f"[论文删除] 论文 {paper_id} 及其向量数据已删除。"
    )

    return success(
        {
            "id": paper_id,
            "deleted": True,
        }
    )
