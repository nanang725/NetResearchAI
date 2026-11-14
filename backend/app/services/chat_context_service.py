import re

from app.schemas.paper import ChatHistoryMessage


MAX_CHAT_HISTORY_MESSAGES = 12

_HISTORY_REFERENCE_MARKERS = (
    "它",
    "这个",
    "那个",
    "通俗解释",
    "上述",
    "前面",
    "刚才",
    "继续",
    "再解释",
    "怎么理解",
    "有什么优点",
    "有什么缺点",
    "为什么",
    "它的",
    "这种方法",
    "这个方法",
    "那为什么",
    "对比一下",
    "那这篇论文",
    "那篇论文",
    "刚才那篇论文",
    "这篇论文自己的",
    "自己的解释",
    "那原文",
    "原文怎么说",
    "回到论文原文",
    "论文原文",
    "论文中是如何解释",
)

_NO_PAPER_SOURCE_MARKERS = (
    "不用根据论文",
    "不用根据这篇论文",
    "不要根据论文",
    "不要根据这篇论文",
    "不需要根据论文",
    "不需要根据这篇论文",
    "不根据这篇论文",
    "不参考论文",
    "不参考这篇论文",
    "不用参考论文",
    "不用参考这篇论文",
    "先不看论文",
    "先不看这篇论文",
    "不需要依据论文",
    "不需要依据这篇论文",
    "不依据论文",
    "不依据这篇论文",
    "不基于论文",
    "不依赖论文",
    "不依赖这篇论文",
    "不用结合论文",
    "不看论文",
    "不看这篇论文",
)

_EXPLICIT_GENERAL_KNOWLEDGE_MARKERS = (
    "通用知识",
)

_PAPER_EVIDENCE_MARKERS = (
    "根据论文",
    "依据论文",
    "那篇论文",
    "刚才那篇论文",
    "回到论文原文",
    "论文原文",
    "论文中是如何解释",
    "论文中",
    "原文",
    "文中",
    "这篇论文",
)

_INTENT_MARKERS = {
    "comparative_analysis": (
        "两篇论文有什么不同",
        "两篇论文有何不同",
        "两篇论文的差异",
        "比较这两篇",
        "比较两篇",
        "比较不同",
        "比较方法",
        "对比两篇",
        "对比方法",
        "对比一下",
        "有什么区别",
        "区别是什么",
        "差异是什么",
        "有哪些不同",
        "异同点",
        "优缺点对比",
    ),
    "academic_writing": (
        "润色这段摘要",
        "润色摘要",
        "润色论文",
        "改写这段摘要",
        "改写摘要",
        "改写论文",
        "撰写摘要",
        "撰写引言",
        "撰写论文",
        "修改摘要",
        "修改论文",
        "写摘要",
        "改成学术表达",
        "学术表达润色",
        "学术写作",
    ),
    "research_assistance": (
        "帮我提出研究问题",
        "帮我设计研究方案",
        "帮我规划研究方案",
        "帮我设计一个实验",
        "帮我设计实验",
        "请帮我设计实验",
        "请帮我制定研究方案",
        "请帮我规划研究",
        "请协助设计实验",
        "请协助制定研究方案",
        "请协助制定研究计划",
        "帮我分析研究方法",
        "帮我进行研究设计",
        "请建议后续研究",
        "请提出后续研究方向",
        "帮我制定研究计划",
    ),
    "simple_explanation": (
        "通俗解释",
        "通俗地解释",
        "简单解释",
        "简单地解释",
        "简单说明",
        "用通俗",
        "通俗地讲",
        "通俗点讲",
        "用简单的话",
        "用简单语言",
        "简单讲解",
        "怎么理解",
        "讲得通俗",
    ),
}

_NEGATED_INTENT_PATTERN = re.compile(
    r"(?:不要|别|不必|不需要|无需|无须|不是|并非|不想)"
    r"[^，。；！？]{0,8}$"
)


def is_history_dependent(query: str) -> bool:
    normalized_query = "".join(query.split()).casefold()
    return any(marker in normalized_query for marker in _HISTORY_REFERENCE_MARKERS)


def _last_relevant_exchange(
    history: list[ChatHistoryMessage],
) -> tuple[str | None, str | None]:
    last_user_index = next(
        (
            index
            for index in range(len(history) - 1, -1, -1)
            if history[index].role == "user"
        ),
        None,
    )
    if last_user_index is None:
        return None, None
    previous_user = history[last_user_index].content
    previous_assistant = next(
        (
            message.content
            for message in reversed(history[last_user_index + 1 :])
            if message.role == "assistant"
        ),
        None,
    )
    return previous_user, previous_assistant


def _extract_topic_anchor(answer: str | None) -> str | None:
    if not answer:
        return None

    text = answer.removeprefix("【通用知识解释（非论文证据）】").strip()
    patterns = (
        (
            r"(?:核心技术|主要方法|技术方案|研究对象|研究内容)"
            r"(?:是|为|包括)\s*([^，。；！？\n]{2,100})",
            False,
        ),
        (
            r"(?:主要)?(?:研究|关注|探讨|介绍|解决)"
            r"(?:的是|的|是|为|了)?\s*([^，。；！？\n]{2,100})",
            False,
        ),
        (
            r"(?:提出|构建|设计)(?:了)?(?:一种|一个|一套)?\s*"
            r"([^，。；！？\n]{2,100})",
            True,
        ),
        (
            r"(?:采用|使用|基于)(?:了|的是|的|是|为)?\s*"
            r"([^，。；！？\n]{2,100})",
            True,
        ),
    )

    matches = []
    for priority, (pattern, split_relation) in enumerate(patterns):
        match = re.search(pattern, text)
        if match:
            matches.append((match.start(), priority, match.group(1), split_relation))

    if matches:
        _, _, candidate, split_relation = min(matches)
    else:
        definition = re.search(
            r"^([^，。；！？\n]{2,100}?)(?:是一种|是一个|是一套)",
            text,
        )
        if not definition:
            return None
        candidate = definition.group(1)
        split_relation = False

    candidate = re.sub(
        r"^(?:(?:的|是|为|包括|一种|一个|一套|基于|采用|使用|"
        r"用于|通过|应用于)\s*)+",
        "",
        candidate.strip(),
    )
    candidate = re.split(r"[，。；！？\n]", candidate, maxsplit=1)[0]
    if split_relation:
        candidate = re.split(
            r"(?:进行|实现|来|从而)\s*",
            candidate,
            maxsplit=1,
        )[0]

    anchor = candidate.strip(" ：:，。；！？")
    if len(anchor) < 2 or anchor in {
        "方法",
        "技术",
        "方案",
        "研究",
        "内容",
        "问题",
        "模型",
        "系统",
        "原理",
        "结果",
    }:
        return None
    return anchor[:100]


def _history_exchanges(
    history: list[ChatHistoryMessage],
) -> list[tuple[str | None, str, str]]:
    exchanges = []
    pending_user: str | None = None
    for message in history:
        if message.role == "user":
            pending_user = message.content
        elif pending_user is not None:
            source = (
                "generic_knowledge"
                if message.content.startswith("【通用知识解释（非论文证据）】")
                else "paper_evidence"
            )
            exchanges.append((pending_user, message.content, source))
            pending_user = None
    return exchanges


def build_conversation_context(
    query: str,
    history: list[ChatHistoryMessage],
    knowledge_source: str,
) -> str | None:
    if not history or not is_history_dependent(query):
        return None

    exchanges = _history_exchanges(history)
    if not exchanges:
        previous_user, _ = _last_relevant_exchange(history)
        return f"最近问题主题：{previous_user}" if previous_user else None

    preferred_source = knowledge_source
    exchange = next(
        (item for item in reversed(exchanges) if item[2] == preferred_source),
        None,
    )
    if exchange is None:
        if preferred_source == "paper_evidence":
            return None
        exchange = exchanges[-1]
    previous_user, previous_answer, _ = exchange
    topic = _extract_topic_anchor(previous_answer)
    if topic:
        return f"当前讨论主题：{topic}"
    if previous_user:
        return f"最近问题主题：{previous_user[:120]}"
    return None


def build_retrieval_query(
    query: str,
    conversation_context: str | None,
) -> str:
    if not conversation_context:
        return query
    return f"{conversation_context}\n当前问题：{query}"


def _has_unnegated_marker(query: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        start = 0
        while True:
            index = query.find(marker, start)
            if index < 0:
                break
            clause_prefix = re.split(r"[，。；！？]", query[:index])[-1]
            if not _NEGATED_INTENT_PATTERN.search(clause_prefix):
                return True
            start = index + 1
    return False


def classify_intent(
    query: str,
    history: list[ChatHistoryMessage],
) -> str:
    normalized_query = "".join(query.split()).casefold()
    for intent, markers in _INTENT_MARKERS.items():
        if _has_unnegated_marker(normalized_query, markers):
            return intent

    if any(marker in normalized_query for marker in _PAPER_EVIDENCE_MARKERS):
        return "paper_qa"

    if is_history_dependent(query):
        previous_user, _ = _last_relevant_exchange(history)
        if previous_user:
            normalized_previous = "".join(previous_user.split()).casefold()
            for intent, markers in _INTENT_MARKERS.items():
                if _has_unnegated_marker(normalized_previous, markers):
                    return intent

    return "paper_qa"


def resolve_knowledge_source(
    query: str,
    history: list[ChatHistoryMessage],
) -> str:
    normalized_query = "".join(query.split()).casefold()

    negative_spans = []
    marker_matches = sorted(
        (
            (start, start + len(marker))
            for marker in _NO_PAPER_SOURCE_MARKERS
            for start in range(len(normalized_query))
            if normalized_query.startswith(marker, start)
        ),
        key=lambda span: (span[0], -(span[1] - span[0])),
    )
    for start, end in marker_matches:
        if any(start < current_end and end > current_start
               for current_start, current_end in negative_spans):
            continue
        prefix = normalized_query[:start]
        if prefix.endswith(("不是", "并非")):
            continue
        negative_spans.append((start, end))

    source_text = list(normalized_query)
    for start, end in negative_spans:
        source_text[start:end] = " " * (end - start)
    source_text = "".join(source_text)

    if any(marker in source_text for marker in _PAPER_EVIDENCE_MARKERS):
        return "paper_evidence"
    if negative_spans:
        return "generic_knowledge"
    if any(
        marker in source_text
        for marker in _EXPLICIT_GENERAL_KNOWLEDGE_MARKERS
    ):
        return "generic_knowledge"

    for message in reversed(history):
        if message.role == "assistant":
            if message.content.startswith("【通用知识解释（非论文证据）】"):
                return "generic_knowledge"
            break
    return "paper_evidence"
