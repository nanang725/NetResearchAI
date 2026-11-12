import hashlib
import os
import fitz  # PyMuPDF
from sqlalchemy.orm import Session
from app.core.exceptions import AppError

from app.models.paper import Paper, PaperPage, PaperChunk

# 确保上传目录存在
UPLOAD_DIR = os.path.join("data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

def ensure_storage():
    """确保上传目录存在（供 main.py 启动时调用）"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)

def create_paper(db: Session, filename: str, content: bytes, content_type: str) -> Paper:
    # 1. 格式校验
    if not filename.lower().endswith(".pdf"):
        raise AppError("仅支持 PDF 格式文件。", code="INVALID_FILE_TYPE", status_code=400)
    
    # 2. 大小校验（50MB）
    if len(content) > 50 * 1024 * 1024:
        raise AppError("文件大小超过 50MB 限制。", code="FILE_TOO_LARGE", status_code=400)
    
    # 3. SHA256 指纹去重（核心！）
    file_hash = hashlib.sha256(content).hexdigest()
    existing = db.query(Paper).filter(Paper.file_hash == file_hash).first()
    if existing:
        raise AppError(f"检测到重复论文！库中已存在：{existing.original_filename}", code="DUPLICATE_PAPER", status_code=409)

    # 4. 保存物理文件
    stored_filename = f"{file_hash}.pdf"
    file_path = os.path.join(UPLOAD_DIR, stored_filename)
    with open(file_path, "wb") as f:
        f.write(content)

    # 5. 解析 PDF
    try:
        doc = fitz.open(file_path)
        metadata = doc.metadata or {}
        
        # 智能提取标题（有些 PDF 第一行就是标题）
        title = metadata.get("title", "").strip()
        if not title or len(title) < 5:
            first_page_text = doc[0].get_text() if len(doc) > 0 else ""
            lines = [line.strip() for line in first_page_text.split('\n') if line.strip()]
            title = lines[0] if lines else filename.rsplit(".", 1)[0]
            
        authors = metadata.get("author", "").strip() or "未知作者"
        page_count = len(doc)
        char_count = 0
        
        # 6. 创建主记录
        paper = Paper(
            title=title[:500], # 防止超长
            file_hash=file_hash,
            original_filename=filename,
            stored_filename=stored_filename,
            file_path=file_path,
            file_size=len(content),
            content_type=content_type or "application/pdf",
            page_count=page_count,
            authors=authors,
            parse_status="parsed",
            index_status="pending"
        )
        db.add(paper)
        db.flush() # 拿到 paper.id

        # 7. 逐页提取文本并智能切块
        chunk_index_global = 0
        
        for page_num in range(page_count):
            page = doc[page_num]
            text = page.get_text("text")
            char_count += len(text)
            
            paper_page = PaperPage(
                paper_id=paper.id,
                page_number=page_num + 1,
                text=text,
                char_count=len(text)
            )
            db.add(paper_page)
            
            # --- 智能文本切块逻辑 ---
            chunk_size = 500
            start = 0
            while start < len(text):
                end = start + chunk_size
                if end < len(text):
                    last_newline = text.rfind('\n', start, end)
                    last_period = text.rfind('。', start, end)
                    last_dot = text.rfind('. ', start, end)
                    cut_point = max(last_newline, last_period, last_dot)
                    
                    if cut_point > start + 100: 
                        end = cut_point + 1
                
                chunk_text = text[start:end].strip()
                if chunk_text:
                    chunk = PaperChunk(
                        paper_id=paper.id,
                        page_number=page_num + 1,
                        chunk_index=chunk_index_global,
                        text=chunk_text
                    )
                    db.add(chunk)
                    chunk_index_global += 1
                
                start = end
            # ---------------------------
            
        paper.char_count = char_count
        doc.close()
        
        db.commit()
        db.refresh(paper)
        return paper
        db.commit()
        db.refresh(paper)
        return paper

    except Exception as e:
        db.rollback()
        if os.path.exists(file_path):
            os.remove(file_path)
        if isinstance(e, AppError):
            raise e
        raise AppError(f"PDF 解析失败: {str(e)}", code="PDF_PARSE_ERROR", status_code=500)


def list_papers(db: Session):
    return db.query(Paper).order_by(Paper.created_at.desc()).all()

def get_paper(db: Session, paper_id: int, with_pages: bool = False):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise AppError("论文不存在", code="NOT_FOUND", status_code=404)
    return paper

def delete_paper(db: Session, paper_id: int):
    paper = get_paper(db, paper_id)
    if os.path.exists(paper.file_path):
        os.remove(paper.file_path)
    db.delete(paper)
    db.commit()

def serialize_paper(paper: Paper, include_pages: bool = False):
    data = {
        "id": paper.id,
        "title": paper.title,
        "original_filename": paper.original_filename,
        "file_size": paper.file_size,
        "page_count": paper.page_count,
        "authors": paper.authors,
        "created_at": paper.created_at.isoformat() if paper.created_at else None,
        "char_count": paper.char_count,
        "index_status": paper.index_status
    }
    if include_pages:
        data["pages"] = [{"page_number": p.page_number, "text": p.text} for p in paper.pages]
    return data

def search_chunks(db: Session, query: str, limit: int = 5):
    """基础关键词检索"""
    if not query.strip():
        return []
    
    # 简单的 SQL LIKE 检索（为了抗幻觉，带上对应的论文标题）
    results = db.query(PaperChunk, Paper).join(Paper, PaperChunk.paper_id == Paper.id)\
        .filter(PaperChunk.text.contains(query))\
        .limit(limit).all()
        
    return [
        {
            "chunk_id": chunk.id,
            "paper_id": chunk.paper_id,
            "paper_title": paper.title,
            "page_number": chunk.page_number,
            "text": chunk.text
        }
        for chunk, paper in results
    ]