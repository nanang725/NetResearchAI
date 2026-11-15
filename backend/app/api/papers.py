
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

import asyncio
import logging
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.responses import success
from app.database import get_db
from app.models.paper import Paper
from app.schemas.paper import (
    AgentChatResponse,
    AgentChatValidationErrorResponse,
    ChatHistoryMessage,
    PaperVersionCreate,
    PaperImportJobListResponse,
    PaperImportJobResponse,
)

from app.services.paper_service import (
    add_paper_version,
    compare_paper_versions,
    create_or_resume_paper,
    delete_paper_with_cleanup,
    get_paper,
    get_paper_parse_completeness,
    list_papers,
    list_paper_versions,
    retain_paper_version,
    serialize_paper,
)
from app.services.ingestion_service import (
    IMPORT_DIRECTORY,
    create_import_job,
    create_index_rebuild_job,
    get_import_job,
    list_import_jobs,
    retry_import_item,
    retry_import_job,
    serialize_import_job,
)

from app.services.llm_service import (
    generate_rag_answer,
    generate_general_knowledge_answer,
    filter_relevant_contexts,
    is_enumeration_query,
)
from app.services.chat_context_service import (
    MAX_CHAT_HISTORY_MESSAGES,
    build_conversation_context,
    build_retrieval_query,
    classify_intent,
    resolve_knowledge_source,
)

from app.services.retrieval_service import (
    search_chunks_hybrid,
)

from app.services.embedding_service import (
    audit_index_consistency,
    find_similar_papers,
    index_paper,
    delete_paper_vectors,
)


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/papers",
    tags=["papers"],
)


def _stage_upload(
    file_handle,
    destination: Path,
    max_bytes: int,
    limit_message: str,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0
    prefix = b""
    created = False
    try:
        file_handle.seek(0)
        with destination.open("xb") as output:
            created = True
            while True:
                chunk = file_handle.read(1024 * 1024)
                if not chunk:
                    break
                if len(prefix) < 4:
                    prefix = (prefix + chunk)[:4]
                total_size += len(chunk)
                if total_size > max_bytes:
                    raise AppError(
                        limit_message,
                        code="FILE_TOO_LARGE",
                        status_code=400,
                    )
                output.write(chunk)
        if not total_size:
            raise AppError("上传文件为空。", code="EMPTY_FILE", status_code=400)
        if not prefix.startswith(b"%PDF"):
            raise AppError("文件内容不是有效的 PDF。", code="INVALID_PDF", status_code=400)
        return total_size
    except Exception:
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove rejected staged upload %s", destination)
        raise


def _safe_import_filename(filename: str | None, file_number: int) -> str:
    if not filename:
        return f"file-{file_number}.pdf"
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    basename = re.sub(r"[\x00-\x1f\x7f]", "_", basename).strip()
    if basename in {"", ".", ".."}:
        return f"file-{file_number}.pdf"
    return basename[:512]


def _failed_staged_file(filename: str, size: int, message: str) -> dict:
    return {
        "filename": filename,
        "size": size,
        "status": "failed",
        "current_stage": "failed",
        "parse_status": "failed",
        "error_message": message,
    }


@router.post(
    "/import-jobs",
    status_code=202,
    response_model=PaperImportJobResponse,
)
async def create_paper_import_job(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not files:
        raise AppError("请选择至少一个 PDF 文件。", code="EMPTY_BATCH", status_code=400)
    if len(files) > settings.max_batch_files:
        raise AppError(
            f"单次最多允许导入 {settings.max_batch_files} 个文件。",
            code="BATCH_TOO_LARGE",
            status_code=400,
        )

    staged_files: list[dict] = []
    staged_paths: list[Path] = []
    staged_total = 0
    try:
        for file_number, upload in enumerate(files, start=1):
            filename = _safe_import_filename(upload.filename, file_number)
            reported_size = upload.size or 0
            if not upload.filename:
                staged_files.append(
                    _failed_staged_file(filename, reported_size, "请选择名称有效的 PDF 文件。")
                )
                continue
            if not filename.lower().endswith(".pdf"):
                staged_files.append(
                    _failed_staged_file(filename, reported_size, "仅支持 PDF 文件。")
                )
                continue
            remaining_batch_bytes = settings.max_batch_upload_mb * 1024 * 1024 - staged_total
            if remaining_batch_bytes <= 0:
                staged_files.append(
                    _failed_staged_file(
                        filename,
                        reported_size,
                        f"批次累计大小超过 {settings.max_batch_upload_mb}MB 限制。",
                    )
                )
                continue
            size_limit = min(settings.max_upload_bytes, remaining_batch_bytes)
            size_message = (
                f"文件大小超过 {settings.max_upload_mb}MB 限制。"
                if size_limit == settings.max_upload_bytes
                else f"批次累计大小超过 {settings.max_batch_upload_mb}MB 限制。"
            )
            destination = (
                settings.upload_path
                / IMPORT_DIRECTORY
                / f"{uuid4()}.pdf"
            )
            try:
                size = await asyncio.to_thread(
                    _stage_upload,
                    upload.file,
                    destination,
                    size_limit,
                    size_message,
                )
            except AppError as exc:
                staged_files.append(_failed_staged_file(filename, reported_size, exc.message))
                continue
            except OSError:
                logger.exception("Could not stage batch upload filename=%s", filename)
                staged_files.append(
                    _failed_staged_file(filename, reported_size, "服务器无法暂存此文件，请稍后重试。")
                )
                continue
            staged_paths.append(destination)
            staged_total += size
            staged_files.append(
                {"path": destination, "filename": filename, "size": size}
            )
        job = create_import_job(db, staged_files)
    except Exception:
        for path in staged_paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        for upload in files:
            await upload.close()
    return success(serialize_import_job(job), status_code=202)


@router.get("/import-jobs", response_model=PaperImportJobListResponse)
def get_paper_import_jobs(db: Session = Depends(get_db)):
    return success(
        [serialize_import_job(job) for job in list_import_jobs(db)]
    )


@router.get("/import-jobs/{job_id}", response_model=PaperImportJobResponse)
def get_paper_import_job(job_id: str, db: Session = Depends(get_db)):
    return success(serialize_import_job(get_import_job(db, job_id)))


@router.post("/import-jobs/{job_id}/retry", response_model=PaperImportJobResponse)
def retry_paper_import_job(job_id: str, db: Session = Depends(get_db)):
    return success(serialize_import_job(retry_import_job(db, job_id)))


@router.post(
    "/import-jobs/{job_id}/items/{item_id}/retry",
    response_model=PaperImportJobResponse,
)
def retry_paper_import_item(
    job_id: str,
    item_id: str,
    db: Session = Depends(get_db),
):
    return success(serialize_import_job(retry_import_item(db, job_id, item_id)))


@router.get("/index/consistency")
def get_index_consistency(db: Session = Depends(get_db)):
    try:
        return success(audit_index_consistency(db))
    except Exception as exc:
        raise AppError(
            "无法检查向量索引，请确认 ChromaDB 和本地依赖可用。",
            code="INDEX_AUDIT_UNAVAILABLE",
            status_code=503,
            details=str(exc),
        ) from exc


@router.post(
    "/index/rebuild",
    status_code=202,
    response_model=PaperImportJobResponse,
)
def request_index_rebuild(db: Session = Depends(get_db)):
    try:
        audit = audit_index_consistency(db)
    except Exception as exc:
        raise AppError(
            "无法完成索引审计，未创建修复任务。",
            code="INDEX_AUDIT_UNAVAILABLE",
            status_code=503,
            details=str(exc),
        ) from exc

    if audit["unverifiable_vector_count"]:
        raise AppError(
            "部分向量无法读取，未创建定向修复任务。",
            code="INDEX_AUDIT_INCOMPLETE",
            status_code=503,
            details={
                "unverifiable_vector_count": audit["unverifiable_vector_count"],
                "read_error_count": audit["read_error_count"],
            },
        )

    if audit.get("parse_incomplete_page_count", 0):
        raise AppError(
            "存在未完成解析的 PDF 页面，不能创建完整索引修复任务。",
            code="PDF_PARSE_INCOMPLETE",
            status_code=409,
            details={
                "parse_incomplete_page_count": audit.get(
                    "parse_incomplete_page_count",
                    0,
                ),
                "parse_affected_paper_ids": audit.get(
                    "parse_affected_paper_ids",
                    [],
                ),
                "parse_issue_details": audit.get("parse_issue_details", []),
            },
        )

    job = create_index_rebuild_job(
        db,
        paper_ids=audit["affected_paper_ids"],
        include_reconciliation=bool(audit["orphan_vector_count"]),
    )
    return success(serialize_import_job(job), status_code=202)


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

    paper = await asyncio.to_thread(
        create_or_resume_paper,
        db=db,
        filename=file.filename,
        content=content,
        content_type=file.content_type or "application/pdf",
        reject_if_complete=True,
    )

    completeness = get_paper_parse_completeness(db, paper.id)
    if not completeness.complete:
        paper.index_status = "failed"
        db.commit()
        db.refresh(paper)
        return success(
            serialize_paper(paper, include_pages=True),
            status_code=201,
        )

    # PDF 上传和解析成功后，自动建立向量索引
    try:
        indexed = await asyncio.to_thread(
            index_paper,
            db=db,
            paper_id=paper.id,
            force=False,
        )
    except Exception:
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


@router.post(
    "/agent/chat",
    response_model=AgentChatResponse,
    responses={
        422: {
            "model": AgentChatValidationErrorResponse,
            "description": "请求参数不合法。",
        }
    },
)
def agent_chat(
    query: str,
    paper_id: int | None = None,
    history: list[ChatHistoryMessage] | None = Body(
        default=None,
        max_length=MAX_CHAT_HISTORY_MESSAGES,
    ),
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

    history = history or []
    intent = classify_intent(query, history)
    knowledge_source = resolve_knowledge_source(query, history)
    conversation_context = build_conversation_context(
        query,
        history,
        knowledge_source,
    )
    retrieval_query = build_retrieval_query(
        query,
        conversation_context,
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
        f"🧭 意图识别完成：{intent}。",
    ]

    if knowledge_source == "generic_knowledge":
        steps.append("🌐 本轮使用通用知识模式，不检索或引用论文证据。")
        answer = generate_general_knowledge_answer(
            query=query,
            conversation_context=conversation_context,
            intent=intent,
        )
        steps.append("✅ 通用知识回答生成完成。")
        return success(
            {
                "answer": answer,
                "steps": steps,
                "contexts": [],
                "is_relevant": False,
                "intent": intent,
                "knowledge_source": knowledge_source,
            }
        )

    if conversation_context:
        steps.append("🧩 已结合最近一轮相关对话理解当前追问。")

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
        and is_enumeration_query(retrieval_query)
    )
    retrieval_limit = 8 if enumeration_query else 5

    # 执行 BM25 + BGE-M3 混合检索
    contexts = search_chunks_hybrid(
        db=db,
        query=retrieval_query,
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
                "intent": intent,
                "knowledge_source": knowledge_source,
            }
        )


    # 对检索结果进行逐条证据相关性过滤
    steps.append(
        "🔍 正在逐条检查检索证据与用户问题的相关性。"
    )

    filtered_contexts = filter_relevant_contexts(
        query=query,
        contexts=contexts,
        contextual_query=retrieval_query,
        conversation_context=conversation_context,
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
                "intent": intent,
                "knowledge_source": knowledge_source,
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
        contextual_query=retrieval_query,
        conversation_context=conversation_context,
        intent=intent,
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
            "intent": intent,
            "knowledge_source": knowledge_source,
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


@router.get("/{paper_id}/similar")
def get_similar_papers(
    paper_id: int,
    db: Session = Depends(get_db),
):
    try:
        candidates = find_similar_papers(db, paper_id)
    except ValueError as exc:
        raise AppError(str(exc), code="NOT_FOUND", status_code=404) from exc
    except RuntimeError as exc:
        raise AppError(
            "语义候选暂不可用，请检查本地 BGE-M3 模型和向量索引。",
            code="SIMILARITY_UNAVAILABLE",
            status_code=503,
            details=str(exc),
        ) from exc
    return success(candidates)


@router.get("/{paper_id}/versions")
def get_paper_versions(paper_id: int, db: Session = Depends(get_db)):
    return success(list_paper_versions(db, paper_id))


@router.post("/{paper_id}/versions")
def register_paper_version(
    paper_id: int,
    request: PaperVersionCreate,
    db: Session = Depends(get_db),
):
    return success(
        add_paper_version(
            db,
            paper_id,
            request.based_on_id,
            request.source,
        )
    )


@router.post("/{paper_id}/versions/{retained_paper_id}/retain")
def retain_version(
    paper_id: int,
    retained_paper_id: int,
    db: Session = Depends(get_db),
):
    return success(retain_paper_version(db, paper_id, retained_paper_id))


@router.get("/{paper_id}/versions/{other_paper_id}/diff")
def compare_versions(
    paper_id: int,
    other_paper_id: int,
    db: Session = Depends(get_db),
):
    return success(compare_paper_versions(db, paper_id, other_paper_id))


@router.delete("/{paper_id}/versions/{version_id}")
def remove_paper_version(
    paper_id: int,
    version_id: int,
    db: Session = Depends(get_db),
):
    versions = list_paper_versions(db, paper_id)
    if version_id not in {item["paper_id"] for item in versions}:
        raise AppError(
            "指定论文不属于当前版本组。",
            code="VERSION_NOT_FOUND",
            status_code=404,
        )
    deletion, cleanup_pending = delete_paper_with_cleanup(
        db=db,
        paper_id=version_id,
        delete_vectors=delete_paper_vectors,
    )
    if cleanup_pending:
        raise AppError(
            "版本记录已删除，但部分外部资源清理失败，服务将在下次启动时重试。",
            code="PAPER_CLEANUP_PENDING",
            status_code=503,
            details={"paper_id": version_id},
        )
    return success({"id": version_id, "deleted": True})


@router.delete("/{paper_id}")
def remove_paper(
    paper_id: int,
    db: Session = Depends(get_db),
):
    deletion, cleanup_pending = delete_paper_with_cleanup(
        db=db,
        paper_id=paper_id,
        delete_vectors=delete_paper_vectors,
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
