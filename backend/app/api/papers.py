from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.responses import success
from app.database import get_db
from app.models.paper import Paper, PaperChunk  # 必须导入模型用于单篇精准检索
from app.services.paper_service import create_paper, delete_paper, get_paper, list_papers, serialize_paper, search_chunks
from app.services.llm_service import generate_rag_answer
from app.services.agent_service import agent_app

router = APIRouter(prefix="/papers", tags=["papers"])


@router.post("/agent/chat")
def agent_chat(query: str, paper_id: int = None, db: Session = Depends(get_db)):
    print(f"\n=== 收到问答请求, paper_id={paper_id}, query={query} ===")
    
    contexts = []
    if paper_id:
        # 【单篇模式】：不搜关键词了，直接强行把这篇文章的前 3 个文本块拿给大模型！
        results = db.query(PaperChunk, Paper).join(Paper, PaperChunk.paper_id == Paper.id)\
            .filter(PaperChunk.paper_id == paper_id).limit(3).all()
        contexts = [{"chunk_id": c.id, "paper_id": c.paper_id, "paper_title": p.title, "page_number": c.page_number, "text": c.text} for c, p in results]
        print(f"单篇模式，强行抓取 {len(contexts)} 条证据")
    else:
        # 【全库模式】：用之前的智能检索
        contexts = search_chunks(db, query, limit=3)
        print(f"全库模式，检索到 {len(contexts)} 条证据")

    # 既然拿到了，直接调用大模型，不经过 LangGraph 的图了（防止它乱拦）
    if not contexts:
        return success({
            "answer": "根据当前知识库，未找到相关证据。",
            "steps": ["❌ 未检索到任何文本块"],
            "contexts": []
        })

    # 强行让大模型总结
    try:
        from app.services.llm_service import generate_rag_answer
        answer = generate_rag_answer(query, contexts)
        steps = [
            f"🤖 意图识别完成：判定为 RETRIEVE 意图",
            f"🔍 锁定范围：{'单篇论文' if paper_id else '全库检索'}",
            f"🧠 提取到 {len(contexts)} 条证据，正在总结...",
            "✅ 回答生成完毕"
        ]
    except Exception as e:
        answer = f"大模型调用失败: {str(e)}"
        steps = ["❌ 大模型生成失败"]

    print(f"=== 问答结束 ===\n")
    return success({
        "answer": answer,
        "steps": steps,
        "contexts": contexts
    })

@router.get("")
def list_all_papers(db: Session = Depends(get_db)):
    papers = list_papers(db)
    return success([serialize_paper(paper, include_pages=False) for paper in papers])


@router.get("/search")
def search_papers(q: str = "", db: Session = Depends(get_db)):
    if not q:
        return success([])
    results = search_chunks(db, q)
    return success(results)


@router.post("/agent/chat")
def agent_chat(query: str, paper_id: int = None, db: Session = Depends(get_db)):
    print(f"\n=== 收到问答请求, paper_id={paper_id}, query={query} ===")
    
    contexts = []
    if paper_id:
        # 【单篇模式】：不管问什么，强行拿这篇论文的前 3 块喂给大模型！
        results = db.query(PaperChunk, Paper).join(Paper, PaperChunk.paper_id == Paper.id)\
            .filter(PaperChunk.paper_id == paper_id).limit(3).all()
        contexts = [{"chunk_id": c.id, "paper_id": c.paper_id, "paper_title": p.title, "page_number": c.page_number, "text": c.text} for c, p in results]
        print(f"单篇模式，强行抓取 {len(contexts)} 条证据")
    else:
        # 【全库模式】：走之前的智能检索
        contexts = search_chunks(db, query, limit=3)
        print(f"全库模式，检索到 {len(contexts)} 条证据")

    if not contexts:
        return success({
            "answer": "根据当前知识库，未找到相关证据。",
            "steps": ["❌ 未检索到任何文本块"],
            "contexts": []
        })

    # 直接调用大模型，不再走 LangGraph 的图
    try:
        from app.services.llm_service import generate_rag_answer
        answer = generate_rag_answer(query, contexts)
        steps = [
            "🤖 意图识别完成：强制检索意图",
            f"🔍 锁定范围：{'单篇论文' if paper_id else '全库检索'}",
            f"🧠 提取到 {len(contexts)} 条证据，正在总结...",
            "✅ 回答生成完毕"
        ]
    except Exception as e:
        answer = f"大模型调用失败: {str(e)}"
        steps = ["❌ 大模型生成失败"]

    print(f"=== 问答结束 ===\n")
    return success({
        "answer": answer,
        "steps": steps,
        "contexts": contexts
    })

@router.get("/{paper_id}")
def get_paper_detail(paper_id: int, db: Session = Depends(get_db)):
    paper = get_paper(db, paper_id, with_pages=True)
    return success(serialize_paper(paper, include_pages=True))


@router.delete("/{paper_id}")
def remove_paper(paper_id: int, db: Session = Depends(get_db)):
    delete_paper(db, paper_id)
    return success({"id": paper_id, "deleted": True})


@router.post("/agent/chat")
def agent_chat(query: str, paper_id: int = None, db: Session = Depends(get_db)):
    print(f"\n=== 收到问答请求, paper_id={paper_id}, query={query} ===")
    
    contexts = []
    if paper_id:
        # 【单篇模式】：不管问什么，强行拿这篇论文的前 3 块喂给大模型！
        results = db.query(PaperChunk, Paper).join(Paper, PaperChunk.paper_id == Paper.id)\
            .filter(PaperChunk.paper_id == paper_id).limit(3).all()
        contexts = [{"chunk_id": c.id, "paper_id": c.paper_id, "paper_title": p.title, "page_number": c.page_number, "text": c.text} for c, p in results]
        print(f"单篇模式，强行抓取 {len(contexts)} 条证据")
    else:
        # 【全库模式】：走智能检索
        contexts = search_chunks(db, query, limit=3)
        print(f"全库模式，检索到 {len(contexts)} 条证据")

    if not contexts:
        return success({
            "answer": "根据当前知识库，未找到相关证据。",
            "steps": ["❌ 未检索到任何文本块"],
            "contexts": []
        })

    try:
        from app.services.llm_service import generate_rag_answer
        answer = generate_rag_answer(query, contexts)
        steps = [
            "🤖 意图识别完成：强制检索意图",
            f"🔍 锁定范围：{'单篇论文' if paper_id else '全库检索'}",
            f"🧠 提取到 {len(contexts)} 条证据，正在总结...",
            "✅ 回答生成完毕"
        ]
    except Exception as e:
        answer = f"大模型调用失败: {str(e)}"
        steps = ["❌ 大模型生成失败"]

    print(f"=== 问答结束 ===\n")
    return success({
        "answer": answer,
        "steps": steps,
        "contexts": contexts
    })