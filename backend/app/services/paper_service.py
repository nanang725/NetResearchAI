import hashlib
import os
import fitz
import difflib
import re
from sqlalchemy.orm import Session
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperPage, PaperChunk

# 确保上传目录存在
UPLOAD_DIR = os.path.join("data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

def ensure_storage():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

def create_paper(db: Session, filename: str, content: bytes, content_type: str) -> Paper:
    # 1. 格式校验
    if not filename.lower().endswith(".pdf"):
        raise AppError("仅支持 PDF 格式文件。", code="INVALID_FILE_TYPE", status_code=400)
    
    # 2. 大小校验（50MB）
    if len(content) > 50 * 1024 * 1024:
        raise AppError("文件大小超过 50MB 限制。", code="FILE_TOO_LARGE", status_code=400)
    
    # 3. SHA256 指纹去重（绝对重复拦截）
    file_hash = hashlib.sha256(content).hexdigest()
    existing_exact = db.query(Paper).filter(Paper.file_hash == file_hash).first()
    if existing_exact:
        raise AppError(f"检测到完全相同的重复文件！库中已存在：{existing_exact.original_filename}", code="DUPLICATE_PAPER", status_code=409)

    # 4. 保存物理文件
    stored_filename = f"{file_hash}.pdf"
    file_path = os.path.join(UPLOAD_DIR, stored_filename)
    with open(file_path, "wb") as f:
        f.write(content)

    # 5. 解析 PDF 并做语义版本去重
    try:
        doc = fitz.open(file_path)
        
        # 提取前 3 页的文本（通常涵盖 Abstract 和 Introduction）
        text_sample = ""
        for i in range(min(3, len(doc))):
            text_sample += doc[i].get_text("text")
            
        # ================= 语义去重核心逻辑（升级版） =================
        # 转小写，仅保留英文和数字，过滤掉所有符号、空格、换行
        text_sample_clean = re.sub(r'[^a-z0-9]', '', text_sample.lower())
        
        if len(text_sample_clean) > 100: 
            existing_papers = db.query(Paper).all()
            for p in existing_papers:
                p_pages = db.query(PaperPage).filter(PaperPage.paper_id == p.id, PaperPage.page_number <= 3).all()
                p_text = "".join([page.text for page in p_pages])
                
                p_text_clean = re.sub(r'[^a-z0-9]', '', p_text.lower())
                if not p_text_clean: 
                    continue

                # 截取前 5000 个有效字符进行比对
                s1 = text_sample_clean[:5000]
                s2 = p_text_clean[:5000]
                
                similarity = difflib.SequenceMatcher(None, s1, s2).ratio()
                
                # 调试打印，你可以在后端黑窗口看到结果
                print(f"[语义去重检测] 正在比对库中论文《{p.title[:20]}...》，相似度为: {similarity*100:.2f}%")
                
                # 阈值设为 0.50
                if similarity > 0.50:
                    raise AppError(
                        f"检测到高度重合的论文版本！库中已有：《{p.title}》。文本相似度高达 {similarity*100:.1f}%。建议保留更新版本。",
                        code="DUPLICATE_VERSION",
                        status_code=409
                    )
        # ====================================================

        # 6. 提取元数据
        metadata = doc.metadata or {}
        title = metadata.get("title", "").strip()
        if not title or len(title) < 5:
            first_page_text = doc[0].get_text() if len(doc) > 0 else ""
            lines = [line.strip() for line in first_page_text.split('\n') if line.strip()]
            title = lines[0] if lines else filename.rsplit(".", 1)[0]
            
        authors = metadata.get("author", "").strip() or "未知作者"
        page_count = len(doc)
        char_count = 0
        
        # 7. 创建主记录
        paper = Paper(
            title=title[:500],
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
        db.flush()

        # 8. 逐页提取文本并智能切块
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
            
            # 文本切块
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
            
        paper.char_count = char_count
        doc.close()
        
        db.commit()
        db.refresh(paper)
        return paper

    except Exception as e:
        db.rollback()
        
        # 关键修复：关闭可能已被打开的 doc 文件句柄，否则 Windows 下删不掉
        try:
            doc.close()
        except:
            pass
            
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
                
        if isinstance(e, AppError):
            raise e
        raise AppError(f"PDF 解析失败: {str(e)}", code="PDF_PARSE_ERROR", status_code=500)

def search_chunks(db: Session, query: str, limit: int = 5):
    if not query.strip():
        return []
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