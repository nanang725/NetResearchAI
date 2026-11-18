from __future__ import annotations

import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.exceptions import AppError


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_TIMEOUT_SECONDS = 15.0
ARXIV_MAX_PAGE_SIZE = 50
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
OPENSEARCH_NS = {"opensearch": "http://a9.com/-/spec/opensearch/1.1/"}
ARXIV_NS = {"arxiv": "http://arxiv.org/schemas/atom"}
FIELD_MAP = {
    "all": "all",
    "title": "ti",
    "abstract": "abs",
    "author": "au",
}
SORT_MAP = {
    "relevance": "relevance",
    "last_updated": "lastUpdatedDate",
    "submitted": "submittedDate",
}
_ARXIV_GATE_LOCK = threading.Lock()
_last_arxiv_request_at = 0.0
_ARXIV_ID_PATTERN = re.compile(
    r"(?:[a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5})",
    re.IGNORECASE,
)


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _ARXIV_ID_PATTERN.search(value)
    return match.group(0) if match else None


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _parse_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _wait_for_rate_limit() -> None:
    global _last_arxiv_request_at
    with _ARXIV_GATE_LOCK:
        elapsed = time.monotonic() - _last_arxiv_request_at
        if elapsed < ARXIV_REQUEST_INTERVAL_SECONDS:
            time.sleep(ARXIV_REQUEST_INTERVAL_SECONDS - elapsed)
        _last_arxiv_request_at = time.monotonic()


def _fetch_atom(params: dict[str, Any]) -> bytes:
    _wait_for_rate_limit()
    try:
        with httpx.Client(
            timeout=ARXIV_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "NetResearchAI/0.5.0 "
                    "(local academic literature discovery MVP)"
                ),
                "Accept": "application/atom+xml, application/xml;q=0.9",
            },
        ) as client:
            response = client.get(ARXIV_API_URL, params=params)
    except httpx.TimeoutException as exc:
        raise AppError(
            "arXiv 服务响应超时，请稍后重试。",
            code="ARXIV_TIMEOUT",
            status_code=504,
        ) from exc
    except httpx.RequestError as exc:
        raise AppError(
            "无法连接 arXiv 服务，请检查网络后重试。",
            code="ARXIV_NETWORK_ERROR",
            status_code=502,
            details={"error_type": type(exc).__name__},
        ) from exc

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise AppError(
            "arXiv 服务请求过于频繁，请稍后重试。",
            code="ARXIV_RATE_LIMITED",
            status_code=429,
            details={"retry_after": retry_after},
        )
    if response.status_code >= 500:
        raise AppError(
            "arXiv 服务暂时不可用，请稍后重试。",
            code="ARXIV_UPSTREAM_ERROR",
            status_code=502,
            details={"upstream_status": response.status_code},
        )
    if response.status_code >= 400:
        raise AppError(
            "arXiv 拒绝了本次搜索请求。",
            code="ARXIV_REQUEST_REJECTED",
            status_code=502,
            details={"upstream_status": response.status_code},
        )
    return response.content


def _entry_text(entry: ET.Element, path: str) -> str | None:
    node = entry.find(path, ATOM_NS)
    return _clean_text(node.text) if node is not None else None


def parse_arxiv_feed(xml_content: bytes) -> dict[str, Any]:
    if b"<!DOCTYPE" in xml_content[:4096].upper() or b"<!ENTITY" in xml_content[
        :4096
    ].upper():
        raise AppError(
            "arXiv 返回了不受支持的 XML 内容。",
            code="ARXIV_INVALID_RESPONSE",
            status_code=502,
        )
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise AppError(
            "arXiv 返回的 Atom 数据无法解析。",
            code="ARXIV_INVALID_RESPONSE",
            status_code=502,
        ) from exc
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise AppError(
            "arXiv 返回的数据不是 Atom feed。",
            code="ARXIV_INVALID_RESPONSE",
            status_code=502,
        )

    total_node = root.find("opensearch:totalResults", OPENSEARCH_NS)
    total_results = (
        int(total_node.text)
        if total_node is not None and (total_node.text or "").strip().isdigit()
        else 0
    )
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = _entry_text(entry, "atom:id") or ""
        title = _entry_text(entry, "atom:title") or ""
        if "/api/errors" in entry_id or title.casefold() == "error":
            raise AppError(
                "arXiv 返回了搜索错误。",
                code="ARXIV_QUERY_ERROR",
                status_code=502,
                details={"message": title[:300]},
            )
        arxiv_id = normalize_arxiv_id(entry_id)
        if not arxiv_id or arxiv_id in seen_ids:
            continue
        seen_ids.add(arxiv_id)
        authors = [
            _clean_text(name.text)
            for author in entry.findall("atom:author", ATOM_NS)
            if (name := author.find("atom:name", ATOM_NS)) is not None
            and (name.text or "").strip()
        ]
        links = entry.findall("atom:link", ATOM_NS)
        abs_url = next(
            (
                link.attrib.get("href")
                for link in links
                if link.attrib.get("rel") == "alternate"
            ),
            f"https://arxiv.org/abs/{arxiv_id}",
        )
        pdf_url = next(
            (
                link.attrib.get("href")
                for link in links
                if (
                    link.attrib.get("title") == "pdf"
                    or link.attrib.get("type") == "application/pdf"
                )
            ),
            f"https://arxiv.org/pdf/{arxiv_id}",
        )
        primary_node = entry.find("arxiv:primary_category", ARXIV_NS)
        primary_category = (
            primary_node.attrib.get("term")
            if primary_node is not None
            else None
        )
        categories = list(
            dict.fromkeys(
                category.attrib["term"]
                for category in entry.findall("atom:category", ATOM_NS)
                if category.attrib.get("term")
            )
        )
        results.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": authors,
                "summary": _entry_text(entry, "atom:summary") or "",
                "published": _parse_datetime(
                    _entry_text(entry, "atom:published")
                ),
                "updated": _parse_datetime(
                    _entry_text(entry, "atom:updated")
                ),
                "primary_category": primary_category,
                "categories": categories,
                "abs_url": abs_url,
                "pdf_url": pdf_url,
                "origin": "arxiv_external",
            }
        )
    return {
        "total_results": total_results,
        "results": results,
    }


def search_arxiv(
    *,
    query: str,
    field: str = "all",
    page: int = 1,
    page_size: int = 10,
    sort_by: str = "relevance",
    sort_order: str = "descending",
) -> dict[str, Any]:
    normalized_query = _clean_text(query)
    if not normalized_query:
        raise AppError(
            "搜索关键词不能为空。",
            code="ARXIV_EMPTY_QUERY",
            status_code=422,
        )
    if field not in FIELD_MAP:
        raise AppError(
            "不支持的 arXiv 搜索字段。",
            code="ARXIV_INVALID_FIELD",
            status_code=422,
        )
    if sort_by not in SORT_MAP:
        raise AppError(
            "不支持的 arXiv 排序方式。",
            code="ARXIV_INVALID_SORT",
            status_code=422,
        )
    if sort_order not in {"ascending", "descending"}:
        raise AppError(
            "不支持的 arXiv 排序方向。",
            code="ARXIV_INVALID_SORT",
            status_code=422,
        )
    if page < 1 or not 1 <= page_size <= ARXIV_MAX_PAGE_SIZE:
        raise AppError(
            "分页参数无效。",
            code="ARXIV_INVALID_PAGINATION",
            status_code=422,
        )
    escaped_query = normalized_query.replace("\\", " ").replace('"', " ")
    parsed = parse_arxiv_feed(
        _fetch_atom(
            {
                "search_query": f'{FIELD_MAP[field]}:"{escaped_query}"',
                "start": (page - 1) * page_size,
                "max_results": page_size,
                "sortBy": SORT_MAP[sort_by],
                "sortOrder": sort_order,
            }
        )
    )
    total_results = parsed["total_results"]
    return {
        "query": normalized_query,
        "field": field,
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "total_results": total_results,
        "total_pages": (
            math.ceil(total_results / page_size) if total_results else 0
        ),
        "results": parsed["results"],
        "source": "arxiv_official_api",
    }


def get_arxiv_paper(arxiv_id: str) -> dict[str, Any]:
    normalized_id = normalize_arxiv_id(arxiv_id)
    if not normalized_id:
        raise AppError(
            "arXiv ID 格式无效。",
            code="ARXIV_INVALID_ID",
            status_code=422,
        )
    parsed = parse_arxiv_feed(
        _fetch_atom(
            {
                "id_list": normalized_id,
                "start": 0,
                "max_results": 1,
            }
        )
    )
    result = next(
        (
            item
            for item in parsed["results"]
            if item["arxiv_id"] == normalized_id
        ),
        None,
    )
    if result is None:
        raise AppError(
            "arXiv 未返回该论文。",
            code="ARXIV_PAPER_NOT_FOUND",
            status_code=404,
            details={"arxiv_id": normalized_id},
        )
    return result
