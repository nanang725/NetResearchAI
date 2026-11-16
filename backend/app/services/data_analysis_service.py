from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import statistics
from collections import Counter
from datetime import date, datetime
from typing import Any

from app.core.exceptions import AppError


MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 20_000
MAX_CSV_COLUMNS = 100
MISSING_MARKERS = {"", "na", "n/a", "null", "none", "nan", "-"}


def _decode_csv(content: bytes) -> str:
    if not content:
        raise AppError(
            "CSV 文件为空。",
            code="CSV_EMPTY",
            status_code=400,
        )
    if b"\x00" in content:
        raise AppError(
            "CSV 文件包含无效的空字节。",
            code="CSV_INVALID_CONTENT",
            status_code=400,
        )
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AppError(
        "CSV 编码无法识别，请使用 UTF-8 或 GB18030。",
        code="CSV_INVALID_ENCODING",
        status_code=400,
    )


def _read_rows(text: str) -> tuple[list[str], list[list[str]]]:
    if not text.strip():
        raise AppError(
            "CSV 文件没有可分析内容。",
            code="CSV_EMPTY",
            status_code=400,
        )
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    stream = io.StringIO(text, newline="")
    try:
        parsed = list(csv.reader(stream, dialect=dialect, strict=True))
    except csv.Error as exc:
        raise AppError(
            "CSV 格式无效，无法解析。",
            code="CSV_INVALID_FORMAT",
            status_code=400,
            details=str(exc),
        ) from exc
    if not parsed:
        raise AppError(
            "CSV 文件缺少表头。",
            code="CSV_EMPTY",
            status_code=400,
        )
    headers = [value.strip() for value in parsed[0]]
    if not headers or not any(headers):
        raise AppError(
            "CSV 第一行必须包含字段名。",
            code="CSV_EMPTY_HEADER",
            status_code=400,
        )
    if any(not value for value in headers):
        raise AppError(
            "CSV 字段名不能为空。",
            code="CSV_INVALID_HEADER",
            status_code=400,
        )
    if len(headers) > MAX_CSV_COLUMNS:
        raise AppError(
            f"CSV 最多支持 {MAX_CSV_COLUMNS} 个字段。",
            code="CSV_TOO_MANY_COLUMNS",
            status_code=413,
        )
    folded = [value.casefold() for value in headers]
    if len(folded) != len(set(folded)):
        raise AppError(
            "CSV 字段名不能重复。",
            code="CSV_DUPLICATE_HEADER",
            status_code=400,
        )

    rows: list[list[str]] = []
    for line_number, raw_row in enumerate(parsed[1:], start=2):
        if not raw_row or all(not value.strip() for value in raw_row):
            continue
        if len(raw_row) != len(headers):
            raise AppError(
                "CSV 数据行列数与表头不一致。",
                code="CSV_ROW_LENGTH_MISMATCH",
                status_code=400,
                details={
                    "line_number": line_number,
                    "expected": len(headers),
                    "actual": len(raw_row),
                },
            )
        rows.append([value.strip() for value in raw_row])
    if not rows:
        raise AppError(
            "CSV 只有表头，没有数据行。",
            code="CSV_NO_DATA_ROWS",
            status_code=422,
        )
    if len(rows) > MAX_CSV_ROWS:
        raise AppError(
            f"CSV 最多支持 {MAX_CSV_ROWS} 行数据。",
            code="CSV_TOO_MANY_ROWS",
            status_code=413,
        )
    if all(
        all(value.strip().casefold() in MISSING_MARKERS for value in row)
        for row in rows
    ):
        raise AppError(
            "CSV 数据行全部为空。",
            code="CSV_NO_DATA_ROWS",
            status_code=422,
        )
    return headers, rows


def _is_missing(value: str) -> bool:
    return value.strip().casefold() in MISSING_MARKERS


def _as_int(value: str) -> int:
    normalized = value.replace(",", "")
    if not re.fullmatch(r"[+-]?\d+", normalized):
        raise ValueError
    return int(normalized)


def _as_float(value: str) -> float:
    normalized = value.replace(",", "")
    number = float(normalized)
    if not math.isfinite(number):
        raise ValueError
    return number


def _as_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "yes", "y"}:
        return True
    if normalized in {"false", "no", "n"}:
        return False
    raise ValueError


def _as_date(value: str) -> date:
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).date()


def _infer_type(values: list[str]) -> str:
    parsers = (
        ("boolean", _as_bool),
        ("integer", _as_int),
        ("number", _as_float),
        ("date", _as_date),
    )
    for type_name, parser in parsers:
        try:
            for value in values:
                parser(value)
        except (ValueError, TypeError, OverflowError):
            continue
        return type_name
    return "text"


def _numeric_values(values: list[str]) -> list[float]:
    parsed = []
    for value in values:
        try:
            parsed.append(_as_float(value))
        except (ValueError, TypeError, OverflowError):
            continue
    return parsed


def _round_optional(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _column_profile(
    name: str,
    raw_values: list[str],
) -> dict[str, Any]:
    present = [value for value in raw_values if not _is_missing(value)]
    missing = len(raw_values) - len(present)
    inferred_type = _infer_type(present) if present else "text"
    numeric = _numeric_values(present)
    statistics_payload: dict[str, Any] = {
        "min": None,
        "max": None,
        "mean": None,
        "median": None,
        "stdev": None,
    }
    if numeric and inferred_type in {"integer", "number"}:
        statistics_payload.update(
            {
                "min": _round_optional(min(numeric)),
                "max": _round_optional(max(numeric)),
                "mean": _round_optional(statistics.mean(numeric)),
                "median": _round_optional(statistics.median(numeric)),
                "stdev": _round_optional(
                    statistics.stdev(numeric) if len(numeric) > 1 else 0.0
                ),
            }
        )
    top_values = [
        {
            "value": value,
            "count": count,
            "ratio": round(count / len(present), 4),
        }
        for value, count in Counter(present).most_common(5)
    ]
    return {
        "name": name,
        "type": inferred_type,
        "count": len(present),
        "missing": missing,
        "missing_rate": round(missing / len(raw_values), 4) if raw_values else 0,
        "unique": len(set(present)),
        "statistics": statistics_payload,
        "top_values": top_values,
    }


def _histogram(
    column: str,
    values: list[float],
    *,
    max_bins: int = 8,
) -> dict[str, Any]:
    low = min(values)
    high = max(values)
    if low == high:
        return {
            "chart_type": "bar",
            "title": f"{column} 数值分布",
            "series_name": "记录数",
            "categories": [f"{low:g}"],
            "values": [len(values)],
            "aggregation": "count",
            "category_column": column,
            "value_column": column,
            "caption": f"上传 CSV 中 {column} 的 {len(values)} 个有效数值相同。",
        }
    bin_count = min(max_bins, max(2, int(math.sqrt(len(values)))))
    width = (high - low) / bin_count
    counts = [0] * bin_count
    for value in values:
        index = min(int((value - low) / width), bin_count - 1)
        counts[index] += 1
    categories = [
        f"{low + index * width:.4g}–{low + (index + 1) * width:.4g}"
        for index in range(bin_count)
    ]
    return {
        "chart_type": "bar",
        "title": f"{column} 数值分布",
        "series_name": "记录数",
        "categories": categories,
        "values": counts,
        "aggregation": "histogram_count",
        "category_column": column,
        "value_column": column,
        "caption": (
            f"基于上传 CSV 的 {len(values)} 个有效 {column} 数值，"
            f"划分为 {bin_count} 个等宽区间。"
        ),
    }


def _build_chart(
    headers: list[str],
    rows: list[list[str]],
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    numeric_columns = [
        profile["name"]
        for profile in profiles
        if profile["type"] in {"integer", "number"}
    ]
    category_columns = [
        profile["name"]
        for profile in profiles
        if (
            profile["type"] in {"text", "boolean", "date"}
            and profile["unique"] > 1
        )
    ]
    header_index = {name: index for index, name in enumerate(headers)}
    for category in category_columns:
        category_index = header_index[category]
        groups: dict[str, list[float]] = {}
        counts_by_group: Counter[str] = Counter()
        for row in rows:
            category_value = row[category_index]
            if _is_missing(category_value):
                continue
            groups.setdefault(category_value, [])
            counts_by_group[category_value] += 1
            if numeric_columns:
                try:
                    groups[category_value].append(
                        _as_float(row[header_index[numeric_columns[0]]])
                    )
                except ValueError:
                    continue
        if not groups:
            continue
        if numeric_columns and any(groups.values()):
            numeric_groups = {
                key: values
                for key, values in groups.items()
                if values
            }
            ordered = sorted(
                numeric_groups.items(),
                key=lambda item: statistics.mean(item[1]),
                reverse=True,
            )[:12]
            categories = [item[0] for item in ordered]
            values = [
                round(statistics.mean(item[1]), 6)
                for item in ordered
            ]
            metric = numeric_columns[0]
            series_name = f"{metric} 均值"
            aggregation = "mean"
        else:
            ordered = sorted(
                groups.items(),
                key=lambda item: counts_by_group[item[0]],
                reverse=True,
            )[:12]
            categories = [item[0] for item in ordered]
            values = [counts_by_group[item[0]] for item in ordered]
            metric = "记录数"
            series_name = "记录数"
            aggregation = "count"
        return {
            "chart_type": "bar",
            "title": f"{metric} 按 {category} 分组",
            "series_name": series_name,
            "categories": categories,
            "values": values,
            "aggregation": aggregation,
            "category_column": category,
            "value_column": numeric_columns[0] if numeric_columns else None,
            "caption": (
                f"基于上传 CSV 的 {len(rows)} 行数据，展示 "
                f"{category} 分组的{series_name}。"
            ),
        }
    if numeric_columns:
        metric = numeric_columns[0]
        values = _numeric_values(
            [row[header_index[metric]] for row in rows]
        )
        if values:
            return _histogram(metric, values)
    raise AppError(
        "CSV 没有足够数据生成真实图表。",
        code="CSV_CHART_UNAVAILABLE",
        status_code=422,
    )


def analyze_csv_bytes(content: bytes, *, filename: str | None = None) -> dict[str, Any]:
    if len(content) > MAX_CSV_BYTES:
        raise AppError(
            f"CSV 文件不能超过 {MAX_CSV_BYTES // (1024 * 1024)}MB。",
            code="CSV_TOO_LARGE",
            status_code=413,
        )
    if filename and not filename.casefold().endswith(".csv"):
        raise AppError(
            "仅支持 .csv 文件。",
            code="CSV_INVALID_EXTENSION",
            status_code=400,
        )
    headers, rows = _read_rows(_decode_csv(content))
    profiles = [
        _column_profile(
            header,
            [row[index] for row in rows],
        )
        for index, header in enumerate(headers)
    ]
    chart = _build_chart(headers, rows, profiles)
    highest = max(
        zip(chart["categories"], chart["values"]),
        key=lambda item: item[1],
    )
    chart["analysis"] = (
        f"在图表中，“{highest[0]}”对应的{chart['series_name']}最高，"
        f"为 {highest[1]:g}。该结果直接由上传 CSV 的 {len(rows)} 行数据计算。"
    )
    chart["data_origin"] = "uploaded_csv_aggregation"
    chart_payload = (
        chart["categories"],
        chart["values"],
        chart["aggregation"],
        len(rows),
    )
    payload_hash = hashlib.sha256(
        repr(chart_payload).encode("utf-8")
    ).hexdigest()
    chart["data_sha256"] = payload_hash
    missing_cells = sum(
        _is_missing(cell)
        for row in rows
        for cell in row
    )
    return {
        "filename": filename or "uploaded.csv",
        "row_count": len(rows),
        "column_count": len(headers),
        "missing_cell_count": missing_cells,
        "fields": [
            {
                "name": profile["name"],
                "type": profile["type"],
                "missing": profile["missing"],
                "missing_rate": profile["missing_rate"],
                "unique": profile["unique"],
            }
            for profile in profiles
        ],
        "profiles": profiles,
        "chart": chart,
        "content_labels": {
            "original_evidence": "用户上传 CSV",
            "generated_content": "图表标题、图注和分析文字由系统按统计结果生成",
            "statistics": "确定性描述统计与聚合结果",
        },
        "disclaimer": (
            "所有统计值和图表数值均来自本次上传的 CSV，"
            "系统未补充或生成外部数据。"
        ),
    }
