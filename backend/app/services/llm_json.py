from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class LLMJSONError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.diagnostics = diagnostics or {}


def response_content(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMJSONError(
            "LLM response did not contain message content.",
            kind="invalid_response_shape",
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMJSONError(
            "LLM returned an empty response.",
            kind="empty_response",
        )
    return content.strip()


def finish_reason(response: Any) -> str | None:
    try:
        value = response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    return str(value) if value is not None else None


def response_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_json_object(content: str) -> dict[str, Any]:
    raw = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        (content or "").strip(),
        flags=re.IGNORECASE,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A complete JSON object surrounded by short prose can be recovered
        # without changing any of the model's substantive content.
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidate = raw[start : end + 1]
            try:
                parsed = json.loads(candidate)
                raw = candidate
            except json.JSONDecodeError:
                raise LLMJSONError(
                    "LLM response was not valid JSON.",
                    kind="json_parse_error",
                    diagnostics={
                        "content_length": len(content or ""),
                        "content_sha256": response_digest(content or ""),
                        "json_error": exc.msg,
                        "json_line": exc.lineno,
                        "json_column": exc.colno,
                    },
                ) from exc
        else:
            raise LLMJSONError(
                "LLM response was not valid JSON.",
                kind="json_parse_error",
                diagnostics={
                    "content_length": len(content or ""),
                    "content_sha256": response_digest(content or ""),
                    "json_error": exc.msg,
                    "json_line": exc.lineno,
                    "json_column": exc.colno,
                },
            ) from exc
    if not isinstance(parsed, dict):
        raise LLMJSONError(
            "LLM JSON response must be an object.",
            kind="json_shape_error",
            diagnostics={
                "content_length": len(raw),
                "content_sha256": response_digest(raw),
                "actual_type": type(parsed).__name__,
            },
        )
    return parsed


def repair_messages(
    *,
    invalid_content: str,
    schema_text: str,
    instruction: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Repair the supplied JSON only. Preserve every substantive "
                "claim, source identifier, paper identifier, page number and "
                "citation. Do not add new facts or sources. Return one JSON "
                "object and no Markdown or prose."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Required structure:\n{schema_text}\n\n"
                f"Additional constraint:\n{instruction}\n\n"
                "Malformed response:\n"
                f"{invalid_content[:30000]}"
            ),
        },
    ]


def safe_diagnostics(
    *,
    stage: str,
    attempt: int,
    content: str | None,
    finish_reason: str | None,
    failure_kind: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "stage": stage,
        "attempt": attempt,
        "failure_kind": failure_kind,
        "finish_reason": finish_reason,
        "content_length": len(content or ""),
        "content_sha256": (
            response_digest(content) if content else None
        ),
    }
    if isinstance(error, LLMJSONError):
        diagnostics.update(error.diagnostics)
    elif error is not None:
        diagnostics["error_type"] = type(error).__name__
    return diagnostics


def classify_llm_exception(error: Exception) -> str:
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    if "timeout" in name or "timed out" in message:
        return "timeout"
    if "ratelimit" in name or "rate limit" in message:
        return "rate_limited"
    if any(
        marker in name
        for marker in (
            "connection",
            "apierror",
            "api_status",
            "upstream",
            "serviceunavailable",
        )
    ):
        return "external_service_error"
    return "internal_service_error"
