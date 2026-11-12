from __future__ import annotations

import json
import re
from pathlib import Path

import fitz

from app.core.exceptions import AppError

PDF_MAGIC = b"%PDF"


def validate_pdf_bytes(content: bytes, filename: str, max_bytes: int) -> None:
    if not filename.lower().endswith(".pdf"):
        raise AppError("仅支持 PDF 文件。", code="INVALID_FILE_TYPE", status_code=400)
    if not content:
        raise AppError("上传文件为空。", code="EMPTY_FILE", status_code=400)
    if len(content) > max_bytes:
        size_mb = max_bytes // (1024 * 1024)
        raise AppError(f"文件过大，最大允许 {size_mb} MB。", code="FILE_TOO_LARGE", status_code=413)
    if not content.lstrip().startswith(PDF_MAGIC):
        raise AppError("文件内容不是有效的 PDF。", code="INVALID_PDF", status_code=400)


def extract_pdf(file_path: Path) -> dict:
    try:
        document = fitz.open(file_path)
    except Exception as exc:
        raise AppError("无法打开 PDF 文件，请确认文件未损坏。", code="PDF_OPEN_FAILED", status_code=400, details=str(exc)) from exc

    try:
        if document.is_encrypted:
            try:
                unlocked = document.authenticate("")
            except Exception:
                unlocked = False
            if not unlocked:
                raise AppError("PDF 已加密，当前版本不支持解析加密文档。", code="PDF_ENCRYPTED", status_code=400)

        metadata = document.metadata or {}
        pages: list[dict] = []
        char_count = 0
        for index, page in enumerate(document):
            text = page.get_text("text") or ""
            pages.append(
                {
                    "page_number": index + 1,
                    "text": text,
                    "char_count": len(text),
                }
            )
            char_count += len(text)

        title = _choose_title(metadata.get("title"), pages, file_path.stem)
        authors = _clean_text(metadata.get("author"))
        return {
            "title": title,
            "authors": authors,
            "page_count": document.page_count,
            "char_count": char_count,
            "pages": pages,
            "pdf_metadata": {k: v for k, v in metadata.items() if v},
        }
    finally:
        document.close()


def _choose_title(meta_title: str | None, pages: list[dict], fallback: str) -> str:
    cleaned = _clean_text(meta_title)
    if cleaned and cleaned.lower() not in {"untitled", "unknown", "microsoft word"}:
        return cleaned[:512]
    first_page = pages[0]["text"] if pages else ""
    for line in first_page.splitlines():
        candidate = _clean_text(line)
        if candidate and len(candidate) >= 8:
            return candidate[:512]
    return fallback[:512] or "未命名论文"


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized or None


def dump_metadata(metadata: dict | None) -> str | None:
    if not metadata:
        return None
    return json.dumps(metadata, ensure_ascii=False)
