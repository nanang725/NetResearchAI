from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.responses import success
from app.database import get_db
# 注意：这里把 search_chunks 导入进来了
from app.services.paper_service import create_paper, delete_paper, get_paper, list_papers, serialize_paper, search_chunks

router = APIRouter(prefix="/papers", tags=["papers"])


@router.get("")
def list_all_papers(db: Session = Depends(get_db)):
    papers = list_papers(db)
    return success([serialize_paper(paper, include_pages=False) for paper in papers])


# ==========================================
# 搜索接口必须放在 /{paper_id} 前面！
# ==========================================
@router.get("/search")
def search_papers(q: str = "", db: Session = Depends(get_db)):
    if not q:
        return success([])
    results = search_chunks(db, q)
    return success(results)


@router.post("/upload")
async def upload_paper(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename:
        raise AppError("缺少文件名。", code="MISSING_FILENAME", status_code=400)
    content = await file.read()
    paper = create_paper(db, file.filename, content, file.content_type)
    return success(serialize_paper(paper, include_pages=True), status_code=201)


@router.get("/{paper_id}")
def get_paper_detail(paper_id: int, db: Session = Depends(get_db)):
    paper = get_paper(db, paper_id, with_pages=True)
    return success(serialize_paper(paper, include_pages=True))


@router.delete("/{paper_id}")
def remove_paper(paper_id: int, db: Session = Depends(get_db)):
    delete_paper(db, paper_id)
    return success({"id": paper_id, "deleted": True})