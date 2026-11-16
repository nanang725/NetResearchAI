from fastapi import APIRouter, File, UploadFile

from app.core.exceptions import AppError
from app.core.responses import success
from app.schemas.writing import CsvAnalysisResponse
from app.services.data_analysis_service import (
    MAX_CSV_BYTES,
    analyze_csv_bytes,
)


router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/analyze", response_model=CsvAnalysisResponse)
async def analyze_uploaded_csv(
    file: UploadFile = File(...),
):
    filename = file.filename or "uploaded.csv"
    try:
        content = await file.read(MAX_CSV_BYTES + 1)
    finally:
        await file.close()
    if len(content) > MAX_CSV_BYTES:
        raise AppError(
            "CSV 文件不能超过 5MB。",
            code="CSV_TOO_LARGE",
            status_code=413,
        )
    return success(analyze_csv_bytes(content, filename=filename))
