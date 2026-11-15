from __future__ import annotations

import json
import logging
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Callable

import fitz

from app.config import get_settings
from app.core.exceptions import AppError

PDF_MAGIC = b"%PDF"
logger = logging.getLogger(__name__)
_REFERENCE_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\s+)?(?:references|bibliography|参考文献|引用文献)\s*$",
    re.IGNORECASE,
)
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S.{0,160}$")
_FIGURE_CAPTION = re.compile(
    r"^\s*(?:fig(?:ure)?\.?\s*\d+\b|图\s*\d+)[.:：]?\s*.*$",
    re.IGNORECASE,
)
_PUBLICATION_DOI = re.compile(r"\b10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
_LABELED_YEAR = re.compile(
    r"\b(?:published|publication|accepted|copyright|发表|出版|录用)\D{0,30}(19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)
_TITLE_NOISE_PATTERNS = (
    re.compile(r"\bdate of (?:publication|current version)\b", re.IGNORECASE),
    re.compile(r"\b10\.\d{4,9}/\S+", re.IGNORECASE),
    re.compile(r"\b(?:doi|digital object identifier)\b", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(
        r"^\s*ieee\s+(?:access|transactions\b|journal\b|communications letters\b)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:vol(?:ume)?|no\.?|issue)\s+\d+\b", re.IGNORECASE),
    re.compile(r"^\s*(?:received|revised|accepted|published)\s*:?", re.IGNORECASE),
    re.compile(r"^\s*(?:copyright\b|©|all rights reserved\b)", re.IGNORECASE),
    re.compile(r"^\s*arxiv\s*:\s*\d{4}\.\d{4,5}", re.IGNORECASE),
    re.compile(
        r"^\s*preparation of papers in two-column format\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:senior\s+)?member,\s*ieee\b", re.IGNORECASE),
    re.compile(r"^\s*\d+\s*$"),
)


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


def inspect_pdf_source(content: bytes) -> dict:
    """Read source identity fields without extracting page content."""
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise AppError(
            "无法解析 PDF 文件，请确认文件完整且未损坏。",
            code="PDF_PARSE_ERROR",
            status_code=400,
            details=str(exc),
        ) from exc
    try:
        if document.is_encrypted or document.needs_pass:
            try:
                unlocked = document.authenticate("")
            except Exception:
                unlocked = False
            if not unlocked:
                raise AppError(
                    "PDF 已加密，当前版本不支持解析加密文档。",
                    code="PDF_ENCRYPTED",
                    status_code=400,
                )
        return {
            "page_count": int(document.page_count),
            "pdf_metadata": document.metadata or {},
        }
    finally:
        document.close()


def derive_pdf_metadata(
    pdf_metadata: dict,
    first_pages_text: str,
    filename: str,
    layout_title: str | None = None,
) -> dict:
    title = _choose_title(
        pdf_metadata.get("title"),
        first_pages_text,
        Path(filename).stem,
        layout_title,
    )
    return {
        "title": title,
        "authors": _clean_text(pdf_metadata.get("author")),
        "publication_metadata": _extract_publication_metadata(
            pdf_metadata,
            first_pages_text,
            title,
        ),
    }


class _PageCallbackError(Exception):
    def __init__(self, cause: Exception):
        self.cause = cause
        super().__init__(str(cause))


def extract_pdf(
    content: bytes,
    filename: str,
    page_callback: Callable[[dict], None] | None = None,
    collect_pages: bool = True,
    start_page: int = 1,
    initial_section_path: list[str] | None = None,
    initial_in_references: bool = False,
    page_start_callback: Callable[[int], None] | None = None,
    document_callback: Callable[[dict], None] | None = None,
    page_selection_callback: Callable[[int], bool] | None = None,
    page_skip_state_callback: (
        Callable[[int], tuple[list[str], bool]] | None
    ) = None,
) -> dict:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise AppError(
            "无法解析 PDF 文件，请确认文件完整且未损坏。",
            code="PDF_PARSE_ERROR",
            status_code=400,
            details=str(exc),
        ) from exc

    try:
        if document.is_encrypted or document.needs_pass:
            try:
                unlocked = document.authenticate("")
            except Exception:
                unlocked = False
            if not unlocked:
                raise AppError(
                    "PDF 已加密，当前版本不支持解析加密文档。",
                    code="PDF_ENCRYPTED",
                    status_code=400,
                )

        metadata = document.metadata or {}
        if start_page < 1 or start_page > max(document.page_count, 1):
            raise AppError(
                f"PDF 检查点起始页无效：{start_page}。",
                code="PDF_CHECKPOINT_PAGE_INVALID",
                status_code=409,
            )
        if document_callback is not None:
            try:
                document_callback(
                    {
                        "page_count": document.page_count,
                        "pdf_metadata": metadata,
                    }
                )
            except Exception as exc:
                raise _PageCallbackError(exc) from exc
        pages: list[dict] = []
        char_count = 0
        has_extractable_text = False
        has_image_only_page = False
        has_vector_only_page = False
        ocr_failed = False
        ocr_deferred = False
        first_page_text = ""
        first_page_layout_title = None
        metadata_text_sample = ""
        section_path: list[str] = list(initial_section_path or [])
        in_references = initial_in_references
        for page_number in range(start_page, document.page_count + 1):
            index = page_number - 1
            page = document[index]
            if (
                page_selection_callback is not None
                and not page_selection_callback(page_number)
            ):
                if page_skip_state_callback is not None:
                    section_path, in_references = page_skip_state_callback(
                        page_number
                    )
                continue
            if page_start_callback is not None:
                try:
                    page_start_callback(page_number)
                except Exception as exc:
                    raise _PageCallbackError(exc) from exc
            text = page.get_text("text") or ""
            has_text = bool(text.strip())
            has_images = bool(page.get_images(full=True))
            ocr_executed = False
            ocr_error = None
            page_status = "text_extracted" if has_text else "empty"

            settings = get_settings()
            should_ocr = (
                has_images
                and len(text.strip()) < settings.ocr_min_text_chars
            )
            if should_ocr:
                has_image_only_page = True
                if not settings.ocr_enabled:
                    ocr_deferred = True
                    page_status = "ocr_disabled"
                elif not shutil.which("tesseract"):
                    ocr_deferred = True
                    ocr_error = "Tesseract executable was not found on PATH."
                    page_status = "ocr_unavailable"
                else:
                    ocr_executed = True
                    try:
                        ocr_text = _run_page_ocr(page, settings)
                        if ocr_text.strip():
                            text = ocr_text
                            has_text = True
                            page_status = "ocr_extracted"
                        else:
                            page_status = "ocr_no_text"
                            ocr_error = "OCR completed but returned no text."
                            ocr_failed = True
                    except Exception as exc:
                        ocr_error = str(exc)[:1000]
                        page_status = "ocr_failed"
                        ocr_failed = True

            has_extractable_text = has_extractable_text or has_text
            if not has_text and not has_images and page.get_drawings():
                has_vector_only_page = True
                page_status = "no_text"
            page_result = {
                "page_number": index + 1,
                "text": text,
                "char_count": len(text),
                "parse_status": page_status,
                "ocr_executed": ocr_executed,
                "ocr_error": ocr_error,
                "checkpoint_layout_title": None,
            }
            if page_number == 1:
                first_page_text = text
                if page_status != "ocr_extracted":
                    first_page_layout_title = _extract_layout_title(page)
                    page_result["checkpoint_layout_title"] = first_page_layout_title
            if page_number <= 2 and len(metadata_text_sample) < 6000:
                metadata_text_sample += text[: 6000 - len(metadata_text_sample)]
            elements, section_path, in_references = _extract_page_elements(
                page,
                text,
                section_path,
                in_references,
            )
            page_result["elements"] = elements
            page_result["checkpoint_section_path"] = list(section_path)
            page_result["checkpoint_in_references"] = in_references
            if page_callback is not None:
                try:
                    page_callback(page_result)
                except Exception as exc:
                    raise _PageCallbackError(exc) from exc
            if collect_pages:
                pages.append(page_result)
            char_count += len(text)

        if ocr_failed and has_extractable_text:
            parse_status = "partial_ocr"
        elif ocr_failed:
            parse_status = "ocr_failed"
        elif has_image_only_page and (ocr_deferred or not has_extractable_text):
            parse_status = "needs_ocr"
        elif has_extractable_text:
            parse_status = "parsed"
        elif has_vector_only_page:
            parse_status = "no_text"
        else:
            parse_status = "empty"

        title = _choose_title(
            metadata.get("title"),
            first_page_text,
            Path(filename).stem,
            first_page_layout_title,
        )
        authors = _clean_text(metadata.get("author"))
        publication_metadata = _extract_publication_metadata(
            metadata,
            metadata_text_sample,
            title,
        )
        return {
            "title": title,
            "authors": authors,
            "page_count": document.page_count,
            "char_count": char_count,
            "pages": pages,
            "pdf_metadata": metadata,
            "publication_metadata": publication_metadata,
            "parse_status": parse_status,
            "layout_title": first_page_layout_title,
        }
    except AppError:
        raise
    except _PageCallbackError as exc:
        raise exc.cause
    except Exception as exc:
        raise AppError(
            "无法解析 PDF 文件，请确认文件完整且未损坏。",
            code="PDF_PARSE_ERROR",
            status_code=400,
            details=str(exc),
        ) from exc
    finally:
        document.close()


def _extract_page_elements(
    page: fitz.Page,
    page_text: str,
    section_path: list[str],
    in_references: bool,
) -> tuple[list[dict], list[str], bool]:
    """Extract page-local structure without retaining image bytes or other pages."""
    page_width = float(page.rect.width)
    text_blocks: list[dict] = []
    try:
        page_dict = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = []
            font_sizes = []
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = "".join(span.get("text", "") for span in spans).strip()
                if line_text:
                    lines.append(line_text)
                    font_sizes.extend(
                        float(span.get("size", 0.0))
                        for span in spans
                        if span.get("text", "").strip()
                    )
            block_text = "\n".join(lines).strip()
            if not block_text:
                continue
            bbox = [float(value) for value in block.get("bbox", ())]
            text_blocks.append(
                {
                    "text": block_text,
                    "bbox": bbox if len(bbox) == 4 else None,
                    "font_size": max(font_sizes, default=0.0),
                    "line_count": len(lines),
                }
            )
    except Exception:
        logger.exception("Could not extract text layout from PDF page %s", page.number + 1)

    tables: list[dict] = []
    try:
        for table in page.find_tables().tables:
            rows = [
                [cell.strip() if cell else "" for cell in row]
                for row in (table.extract() or [])
            ]
            if not any(any(cell for cell in row) for row in rows):
                continue
            table_text = "\n".join(" | ".join(row) for row in rows)
            tables.append(
                {
                    "text": table_text,
                    "rows": rows,
                    "bbox": [float(value) for value in table.bbox],
                }
            )
    except Exception:
        logger.exception("Could not detect tables on PDF page %s", page.number + 1)

    images: list[dict] = []
    try:
        for image in page.get_image_info(xrefs=True):
            bbox = [float(value) for value in image.get("bbox", ())]
            width = bbox[2] - bbox[0] if len(bbox) == 4 else 0.0
            height = bbox[3] - bbox[1] if len(bbox) == 4 else 0.0
            # Word/CNKI PDFs can expose every glyph-sized vector object as a
            # separate image. Keeping those as figures floods PaperElement and
            # makes later indexing work on parser noise instead of document
            # structure.
            if len(bbox) == 4 and width > 0 and height > 0 and width * height >= 16.0:
                images.append(
                    {
                        "text": "",
                        "bbox": bbox,
                        "data": {
                            "width": int(image.get("width", 0)),
                            "height": int(image.get("height", 0)),
                            "xref": int(image.get("xref", 0)),
                        },
                    }
                )
    except Exception:
        logger.exception("Could not inspect images on PDF page %s", page.number + 1)

    items: list[tuple[float, float, str, dict]] = []
    for block in text_blocks:
        bbox = block["bbox"] or [0.0, 0.0, page_width, 0.0]
        items.append((bbox[1], bbox[0], "text", block))
    for table in tables:
        items.append((table["bbox"][1], table["bbox"][0], "table", table))
    for image in images:
        items.append((image["bbox"][1], image["bbox"][0], "figure", image))
    items = _order_page_items(items, page_width)

    elements: list[dict] = []
    figure_indices: list[tuple[float, int]] = []
    for y, _x, kind, item in items:
        if kind == "figure":
            figure_indices.append((y, len(elements)))
            elements.append(
                {
                    "element_type": "figure",
                    "text": "",
                    "data": item["data"],
                    "bbox": item["bbox"],
                    "section_path": " > ".join(section_path) or None,
                    "extraction_status": "metadata_only",
                    "confidence": None,
                }
            )
            continue
        if kind == "table":
            elements.append(
                {
                    "element_type": "table",
                    "text": item["text"],
                    "data": {"rows": item["rows"]},
                    "bbox": item["bbox"],
                    "section_path": " > ".join(section_path) or None,
                    "extraction_status": "extracted",
                    "confidence": 1.0,
                }
            )
            continue

        block_text = item["text"]
        bbox = item["bbox"]
        if any(
            _bbox_overlap_ratio(bbox, table["bbox"]) > 0.5
            for table in tables
        ):
            continue
        normalized_lines = [line.strip() for line in block_text.splitlines() if line.strip()]
        if not normalized_lines:
            continue
        is_reference_heading = len(normalized_lines) == 1 and bool(
            _REFERENCE_HEADING.match(normalized_lines[0])
        )
        if is_reference_heading:
            in_references = True
            section_path = [normalized_lines[0]]
            elements.append(
                _make_text_element(
                    "section",
                    block_text,
                    bbox,
                    section_path,
                    "heuristic",
                    0.85,
                )
            )
            continue
        if _is_section_heading(item):
            heading = normalized_lines[0]
            depth = _heading_depth(heading)
            section_path = section_path[: depth - 1] + [heading]
            elements.append(
                _make_text_element(
                    "section",
                    block_text,
                    bbox,
                    section_path,
                    "heuristic",
                    0.75,
                )
            )
            continue

        if in_references:
            element_type = "reference"
            status, confidence = "extracted", 0.85
        elif _FIGURE_CAPTION.match(normalized_lines[0]):
            element_type = "figure_caption"
            nearby = min(figure_indices, key=lambda figure: abs(figure[0] - y), default=None)
            caption_data = (
                {"figure_element_index": nearby[1]}
                if nearby is not None and abs(nearby[0] - y) <= page.rect.height * 0.25
                else {}
            )
            elements.append(
                _make_text_element(
                    element_type,
                    block_text,
                    bbox,
                    section_path,
                    "heuristic",
                    0.8,
                    caption_data,
                )
            )
            continue
        elif _looks_like_display_formula(block_text):
            element_type = "formula"
            status, confidence = "heuristic_text", 0.55
        else:
            element_type = "paragraph"
            status, confidence = "extracted", None
        elements.append(
            _make_text_element(
                element_type,
                block_text,
                bbox,
                section_path,
                status,
                confidence,
            )
        )

    if not text_blocks and page_text.strip():
        elements.append(
            _make_text_element(
                "paragraph",
                page_text.strip(),
                None,
                section_path,
                "ocr_text" if not page.get_text("text").strip() else "extracted",
                None,
            )
        )
    return elements, section_path, in_references


def _order_page_items(items: list[tuple[float, float, str, dict]], page_width: float):
    text_columns = sorted(
        item[1]
        for item in items
        if item[2] == "text" and item[3].get("bbox")
    )
    if len(text_columns) < 4:
        return sorted(items, key=lambda item: (item[0], item[1]))
    gaps = [
        (text_columns[index + 1] - text_columns[index], index)
        for index in range(len(text_columns) - 1)
    ]
    gap, split_index = max(gaps, default=(0.0, 0))
    if gap < page_width * 0.12:
        return sorted(items, key=lambda item: (item[0], item[1]))
    split_x = (text_columns[split_index] + text_columns[split_index + 1]) / 2
    return sorted(
        items,
        key=lambda item: (
            0 if item[3].get("bbox", [0.0, 0.0, page_width, 0.0])[2] - item[1] > page_width * 0.7
            else 1 + (0 if item[1] < split_x else 1),
            item[0],
            item[1],
        ),
    )


def _bbox_overlap_ratio(first: list[float] | None, second: list[float]) -> float:
    if not first or len(first) != 4 or len(second) != 4:
        return 0.0
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection = width * height
    area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    return intersection / area if area else 0.0


def _is_section_heading(block: dict) -> bool:
    if block["line_count"] != 1:
        return False
    text = block["text"].strip()
    if len(text) > 180 or text.endswith((".", "。", ";", "；")):
        return False
    return bool(_NUMBERED_HEADING.match(text))


def _heading_depth(heading: str) -> int:
    match = _NUMBERED_HEADING.match(heading)
    if not match:
        return 1
    return min(match.group(1).count(".") + 1, 6)


def _looks_like_display_formula(text: str) -> bool:
    value = " ".join(text.split())
    if not value or len(value) > 320:
        return False
    operators = sum(value.count(character) for character in "=∑∫√≈≤≥×÷±∏∂")
    words = re.findall(r"[A-Za-z]{2,}", value)
    return operators >= 2 and len(words) <= 10


def _make_text_element(
    element_type: str,
    text: str,
    bbox: list[float] | None,
    section_path: list[str],
    extraction_status: str,
    confidence: float | None,
    data: dict | None = None,
) -> dict:
    return {
        "element_type": element_type,
        "text": text,
        "data": data,
        "bbox": bbox,
        "section_path": " > ".join(section_path) or None,
        "extraction_status": extraction_status,
        "confidence": confidence,
    }


def _extract_publication_metadata(
    metadata: dict,
    first_pages_text: str,
    title: str,
) -> dict:
    doi_value = next(
        (
            match.group(0).rstrip(".,;)")
            for match in _PUBLICATION_DOI.finditer(
                " ".join(
                    str(value)
                    for value in (metadata.values() or [])
                )
                + "\n"
                + first_pages_text
            )
        ),
        None,
    )
    year_match = _LABELED_YEAR.search(first_pages_text)
    if year_match is None:
        for key in ("year", "publication_year", "published"):
            value = metadata.get(key)
            if value and re.fullmatch(r"(?:19|20)\d{2}", str(value).strip()):
                year_match = re.search(r"((?:19|20)\d{2})", str(value).strip())
                break
    venue = next(
        (
            _clean_text(str(metadata.get(key)))
            for key in ("venue", "journal", "conference", "publication_name")
            if metadata.get(key)
        ),
        None,
    )
    normalized_title = "".join(
        character
        for character in unicodedata.normalize("NFKC", title).casefold()
        if character.isalnum()
    )
    normalized_venue = (
        "".join(
            character
            for character in unicodedata.normalize("NFKC", venue).casefold()
            if character.isalnum()
        )
        if venue
        else None
    )
    return {
        "doi": doi_value.casefold() if doi_value else None,
        "normalized_title": normalized_title or None,
        "publication_year": int(year_match.group(1)) if year_match else None,
        "venue": venue,
        "normalized_venue": normalized_venue,
    }


def _choose_title(
    meta_title: str | None,
    first_page_text: str,
    fallback: str,
    layout_title: str | None = None,
) -> str:
    candidate = _clean_title_text(meta_title or "")
    if _is_plausible_title(candidate):
        return candidate[:512]
    candidate = _clean_title_text(layout_title or "")
    if _is_plausible_title(candidate):
        return candidate[:512]
    for line in first_page_text.splitlines():
        candidate = _clean_title_text(line)
        if _is_plausible_title(candidate):
            return candidate[:512]
    candidate = _clean_title_text(fallback)
    return candidate[:512] or "未命名论文"


def _extract_layout_title(page: fitz.Page) -> str | None:
    candidates: list[tuple[float, int, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        lines: list[tuple[float, float, str]] = []
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _clean_title_text("".join(span.get("text", "") for span in spans))
            if not text or _is_title_noise(text):
                continue
            y_position = min(span["bbox"][1] for span in spans)
            if y_position > page.rect.height * 0.42:
                continue
            font_size = max(span.get("size", 0.0) for span in spans)
            lines.append((font_size, y_position, text))
        if not lines:
            continue
        largest_font = max(font_size for font_size, _, _ in lines)
        title_lines = [
            text
            for font_size, _, text in lines
            if font_size >= largest_font - 1.0
        ]
        if largest_font < 13.0:
            title_lines = title_lines[:1]
        else:
            title_lines = title_lines[:4]
        candidate = _clean_title_text(" ".join(title_lines))
        if _is_plausible_title(candidate):
            first_line_y = min(y for _, y, _ in lines)
            candidates.append((largest_font, len(candidate), -first_line_y, candidate))
    if not candidates:
        return None
    return max(candidates)[3]


def _clean_title_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n-–—:;,.•")


def _is_title_noise(value: str) -> bool:
    return any(pattern.search(value) for pattern in _TITLE_NOISE_PATTERNS)


def _is_plausible_title(value: str) -> bool:
    if len(value) < 5 or _is_title_noise(value):
        return False
    if value.casefold() in {"untitled", "unknown", "document", "paper"}:
        return False
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", value))
    if (
        re.search(r"[A-Za-z]", value)
        and len(value.split()) < 3
        and not has_cjk
    ):
        return False
    return True


def _run_page_ocr(page: fitz.Page, settings) -> str:
    text_page = page.get_textpage_ocr(
        language=settings.ocr_language,
        dpi=settings.ocr_dpi,
        full=False,
    )
    return page.get_text("text", textpage=text_page) or ""


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized or None


def dump_metadata(metadata: dict | None) -> str | None:
    if not metadata:
        return None
    return json.dumps(metadata, ensure_ascii=False)
