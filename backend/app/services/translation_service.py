from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperElement, PaperPage
from app.services.llm_service import get_llm_client


TranslationDirection = Literal["zh_to_en", "en_to_zh"]
MAX_TRANSLATION_CHARS = 12_000
TRANSLATION_DISCLAIMER = (
    "翻译结果仅为辅助阅读内容，不是论文原文，也不会参与论文证据检索。"
)

_PLACEHOLDER_PATTERN = re.compile(r"\[\[NRPH_(\d{4})\]\]")
_SENSITIVE_PATTERNS = (
    re.compile(r"https?://[^\s<>\"'，。；]+", re.IGNORECASE),
    re.compile(r"\b10\.\d{4,9}/[^\s,;，。；]+", re.IGNORECASE),
    re.compile(r"\barXiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE),
    re.compile(r"\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]"),
    re.compile(r"\${1,2}[^$\n]{1,500}\${1,2}"),
    re.compile(r"\\\([^)\n]{1,500}\\\)|\\\[[^\]\n]{1,500}\\\]"),
    re.compile(
        r"(?<!\w)[A-Za-z][A-Za-z0-9_]*\s*=\s*[^,.;\n]{1,200}(?!\w)"
    ),
    re.compile(r"\b[A-Z]{2,}[A-Z0-9_-]*\b"),
    re.compile(
        r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,4}\b"
    ),
    re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?:%|‰)?(?![\w])"),
)


@dataclass(frozen=True)
class ProtectedTranslationText:
    text: str
    tokens: tuple[tuple[str, str], ...]


def _ranges_overlap(
    first: tuple[int, int],
    second: tuple[int, int],
) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def protect_sensitive_text(text: str) -> ProtectedTranslationText:
    """Replace retrieval-sensitive literals with stable placeholders."""
    matches: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in _SENSITIVE_PATTERNS:
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if any(_ranges_overlap(span, existing) for existing in occupied):
                continue
            occupied.append(span)
            matches.append((*span, match.group(0)))

    matches.sort(key=lambda item: item[0])
    tokens: list[tuple[str, str]] = []
    pieces: list[str] = []
    cursor = 0
    for index, (start, end, value) in enumerate(matches, start=1):
        placeholder = f"[[NRPH_{index:04d}]]"
        pieces.append(text[cursor:start])
        pieces.append(placeholder)
        cursor = end
        tokens.append((placeholder, value))
    pieces.append(text[cursor:])
    return ProtectedTranslationText(
        text="".join(pieces),
        tokens=tuple(tokens),
    )


def restore_sensitive_text(
    translated_text: str,
    tokens: tuple[tuple[str, str], ...],
) -> str:
    """Validate every protected placeholder and restore its original literal."""
    for placeholder, _value in tokens:
        count = translated_text.count(placeholder)
        if count != 1:
            raise AppError(
                "翻译结果未能完整保留公式、数字、引用或专有标识。",
                code="TRANSLATION_PROTECTION_FAILED",
                status_code=502,
                details={
                    "placeholder": placeholder,
                    "occurrences": count,
                },
            )
    restored = translated_text
    for placeholder, value in tokens:
        restored = restored.replace(placeholder, value)
    return restored


def _translation_instruction(direction: TranslationDirection) -> tuple[str, str]:
    if direction == "zh_to_en":
        return "中文", "英文"
    return "英文", "中文"


def _build_translation_prompt(
    text: str,
    direction: TranslationDirection,
    *,
    scope: str,
) -> tuple[str, str]:
    source_language, target_language = _translation_instruction(direction)
    system_prompt = (
        "你是严谨的计算机网络学术论文翻译器。"
        "只翻译用户给出的文本，不添加解释、总结、评论或新事实。"
    )
    user_prompt = f"""
请将下方{source_language}学术文本翻译为{target_language}。

【翻译范围】
{scope}

【必须遵守】
1. 保持学术含义、论证关系和段落结构，不扩写、不删减、不补充。
2. 所有形如 [[NRPH_0001]] 的占位符必须原样保留且只出现一次。
3. 专有名词、技术术语、作者名、机构名原则上保持原文；已有缩写不要擅自展开。
4. 公式、数字、单位、引用编号、DOI、URL 和标识符不得改写。
5. 只输出翻译正文，不要输出规则、解释或 Markdown 代码围栏。

【待翻译原文】
{text}

【翻译要求】
返回完整{target_language}译文。
""".strip()
    return system_prompt, user_prompt


def _call_translation_model(
    protected_text: str,
    direction: TranslationDirection,
    *,
    scope: str,
) -> tuple[str, str]:
    settings = get_settings()
    system_prompt, user_prompt = _build_translation_prompt(
        protected_text,
        direction,
        scope=scope,
    )
    try:
        client = get_llm_client()
        response = client.chat.completions.create(
            model=settings.llm_model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=5000,
            timeout=45,
        )
    except AppError:
        raise
    except Exception as exc:
        exception_name = type(exc).__name__.casefold()
        raise AppError(
            (
                "翻译服务响应超时，请稍后重试。"
                if "timeout" in exception_name
                else "翻译服务暂时不可用，请稍后重试。"
            ),
            code=(
                "TRANSLATION_TIMEOUT"
                if "timeout" in exception_name
                else "TRANSLATION_LLM_ERROR"
            ),
            status_code=504 if "timeout" in exception_name else 502,
            details={"provider_error": type(exc).__name__},
        ) from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise AppError(
            "翻译服务返回了无法识别的结果。",
            code="TRANSLATION_INVALID_RESPONSE",
            status_code=502,
            details={"provider_error": type(exc).__name__},
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise AppError(
            "翻译服务未返回有效文本。",
            code="TRANSLATION_EMPTY_RESULT",
            status_code=502,
        )
    return content.strip(), settings.llm_model_name


def translate_paper_content(
    db: Session,
    *,
    paper_id: int,
    page_number: int,
    direction: TranslationDirection,
    element_id: int | None = None,
) -> dict:
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if paper is None:
        raise AppError(
            "论文不存在。",
            code="PAPER_NOT_FOUND",
            status_code=404,
        )

    page = (
        db.query(PaperPage)
        .filter(
            PaperPage.paper_id == paper_id,
            PaperPage.page_number == page_number,
        )
        .first()
    )
    if page is None:
        raise AppError(
            f"论文第 {page_number} 页不存在。",
            code="PAGE_NOT_FOUND",
            status_code=404,
        )

    scope = "page"
    source_text = page.text.strip()
    if element_id is not None:
        element = (
            db.query(PaperElement)
            .filter(
                PaperElement.id == element_id,
                PaperElement.paper_id == paper_id,
                PaperElement.page_id == page.id,
                PaperElement.page_number == page_number,
            )
            .first()
        )
        if element is None:
            raise AppError(
                "指定元素不存在，或不属于当前论文页面。",
                code="ELEMENT_NOT_FOUND",
                status_code=404,
            )
        scope = "element"
        source_text = element.text.strip()

    if not source_text:
        raise AppError(
            "当前范围没有可翻译文本。",
            code="NO_TRANSLATABLE_TEXT",
            status_code=400,
        )
    if len(source_text) > MAX_TRANSLATION_CHARS:
        raise AppError(
            f"翻译文本超过 {MAX_TRANSLATION_CHARS} 字符限制，请改为单元素翻译。",
            code="TRANSLATION_TEXT_TOO_LONG",
            status_code=413,
            details={
                "text_length": len(source_text),
                "max_chars": MAX_TRANSLATION_CHARS,
            },
        )

    protected = protect_sensitive_text(source_text)
    translated_protected, model_name = _call_translation_model(
        protected.text,
        direction,
        scope="整页" if scope == "page" else "单个结构化元素",
    )
    translated_text = restore_sensitive_text(
        translated_protected,
        protected.tokens,
    )
    source_language, target_language = _translation_instruction(direction)
    return {
        "paper_id": paper_id,
        "page_number": page_number,
        "element_id": element_id,
        "scope": scope,
        "direction": direction,
        "source_language": source_language,
        "target_language": target_language,
        "original_text": source_text,
        "translated_text": translated_text,
        "protected_token_count": len(protected.tokens),
        "model": model_name,
        "disclaimer": TRANSLATION_DISCLAIMER,
    }


def translate_academic_content(
    *,
    text: str,
    direction: TranslationDirection,
) -> dict:
    source_text = (text or "").strip()
    if not source_text:
        raise AppError(
            "没有可翻译的学术内容。",
            code="NO_TRANSLATABLE_TEXT",
            status_code=400,
        )
    if len(source_text) > MAX_TRANSLATION_CHARS:
        raise AppError(
            f"翻译文本超过 {MAX_TRANSLATION_CHARS} 字符限制。",
            code="TRANSLATION_TEXT_TOO_LONG",
            status_code=413,
            details={
                "text_length": len(source_text),
                "max_chars": MAX_TRANSLATION_CHARS,
            },
        )
    protected = protect_sensitive_text(source_text)
    translated_protected, model_name = _call_translation_model(
        protected.text,
        direction,
        scope="系统生成的学术写作内容",
    )
    translated_text = restore_sensitive_text(
        translated_protected,
        protected.tokens,
    )
    source_language, target_language = _translation_instruction(direction)
    return {
        "direction": direction,
        "source_language": source_language,
        "target_language": target_language,
        "original_text": source_text,
        "translated_text": translated_text,
        "protected_token_count": len(protected.tokens),
        "model": model_name,
        "disclaimer": TRANSLATION_DISCLAIMER,
    }
