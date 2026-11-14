
from openai import OpenAI

from app.config import get_settings
from app.core.exceptions import AppError

import json
def get_llm_client():
    """创建大模型客户端。"""
    settings = get_settings()

    if not settings.llm_api_key:
        raise AppError(
            "未配置 LLM_API_KEY，请检查 .env 文件",
            code="LLM_NOT_CONFIGURED",
            status_code=500
        )

    return OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url
    )


def build_context_text(contexts: list[dict]) -> str:
    """
    将检索得到的论文证据整理成统一格式。
    每条证据保留论文标题、页码和原文。
    """
    context_parts = []

    for i, context in enumerate(contexts):
        paper_title = context.get("paper_title", "未知论文")
        page_number = context.get("page_number", "未知")
        content = context.get("text", "")

        context_parts.append(
            f"[证据 {i + 1}]\n"
            f"论文标题：{paper_title}\n"
            f"页码：第 {page_number} 页\n"
            f"原文内容：\n{content}\n"
        )

    return "\n".join(context_parts)


def build_rag_prompt(query: str, contexts: list[dict]) -> str:
    """
    根据用户问题和论文证据构建 RAG 提示词。
    兼容单篇论文问答、全库问答和多论文比较。
    """
    context_text = build_context_text(contexts)

    paper_titles = list(dict.fromkeys(
        context.get("paper_title", "未知论文")
        for context in contexts
    ))

    paper_count = len(paper_titles)

    if paper_count > 1:
        task_instruction = """
当前检索证据涉及多篇论文。

如果用户的问题涉及论文比较、研究主题、研究目标、技术路线或研究方法，
请按照以下结构组织回答：

一、逐篇论文分析

对于每篇论文，分别分析：

1. 研究主题：论文主要研究什么领域或问题。
2. 研究目标：作者希望解决什么问题，达到什么目的。
3. 核心技术与研究方法：论文采用了哪些关键技术、模型、算法或研究方法。
4. 主要贡献：论文提出了什么方案，取得了什么成果。

注意：不同论文的分析必须分别进行，不要将一篇论文的技术或研究目标错误地归入另一篇论文。

二、论文之间的比较

根据现有证据，从以下角度比较：

1. 研究主题的异同。
2. 研究目标的异同。
3. 核心技术与研究方法的异同。
4. 研究侧重点和主要贡献的区别。

三、总结

概括两篇或多篇论文之间的主要联系和区别。

如果用户只询问某一个具体问题，则围绕问题回答，不必强行使用以上全部结构。
"""
    else:
        task_instruction = """
当前检索证据主要来自一篇论文。

请围绕用户的问题，提取相关研究内容并组织回答。
可以根据问题分析论文的研究主题、研究目标、核心技术、
研究方法、主要贡献或实验结果。

不要强行进行多篇论文比较。
"""

    prompt = f"""
你是一名严谨的计算机网络科研助手，擅长学术论文解析、
文献检索、研究内容总结和多论文对比。

你的回答必须严格依据下方提供的论文原文证据。

【用户问题】
{query}

【检索到的论文证据】
{context_text}

【当前证据涉及的论文】
{chr(10).join(f"- {title}" for title in paper_titles)}

【回答任务】
{task_instruction}

【必须遵守的规则】

1. 事实依据
只能使用检索证据中明确提供的信息回答。
不得根据自己的常识或已有知识补充论文中没有出现的研究内容。

2. 区分论文
必须准确区分不同论文的研究主题、研究目标和技术方法。
不得将不同论文的内容混淆。

3. 证据不足
如果当前证据无法支持某项结论，必须明确说明：
“当前检索到的证据不足以确定这一点。”
不能将合理推测写成论文已经明确提出的结论。

4. 引用来源
涉及论文事实、技术方法、研究结论时，应尽可能标注证据序号。
例如：
Transformer 使用多头注意力机制处理序列信息 [1]。

回答末尾应提供证据来源，格式如下：
- [1] Attention Is All You Need，第 9 页。
- [2] 某某论文，第 4 页。

5. 学术表达
使用准确、清晰的中文表达。
解释专业术语时，可以用简洁语言说明其作用。
避免空泛的评价和没有证据支持的结论。

6. 不要误读证据
检索片段可能只是论文的一部分。
不要因为没有检索到某项内容，就断言整篇论文完全没有涉及该内容。

请直接回答用户问题，不要复述以上规则。
"""

    return prompt


def generate_rag_answer(query: str, contexts: list[dict]) -> str:
    """
    根据用户问题和检索证据生成 RAG 回答。
    支持单篇论文问答和多篇论文综合分析。
    """
    settings = get_settings()
    client = get_llm_client()

    if not contexts:
        return "根据当前知识库，未找到相关证据，暂时无法回答这个问题。"

    prompt = build_rag_prompt(query, contexts)

    try:
        response = client.chat.completions.create(
            model=settings.llm_model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一名严谨的学术科研助手。"
                        "必须以用户提供的论文证据为依据，"
                        "不得编造论文内容，不得混淆不同论文。"
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.1,
        )

        answer = response.choices[0].message.content

        if not answer:
            raise AppError(
                "大模型未返回有效回答",
                code="LLM_EMPTY_RESPONSE",
                status_code=500
            )

        return answer.strip()

    except AppError:
        raise

    except Exception as e:
        raise AppError(
            f"大模型调用失败: {str(e)}",
            code="LLM_ERROR",
            status_code=500
        )


def check_context_relevance(
    query: str,
    contexts: list[dict],
) -> bool:
    """
    判断检索到的论文证据是否与用户问题实质相关。

    True：证据能够回答用户问题的全部或部分内容。
    False：证据与用户问题无实质关系。
    """
    settings = get_settings()
    client = get_llm_client()

    if not contexts:
        return False

    context_text = build_context_text(contexts)

    prompt = f"""
你是一名严谨的学术检索相关性评估助手。

请判断下面的论文证据是否与用户问题具有实质性的内容相关性。

【用户问题】
{query}

【检索到的论文证据】
{context_text}

【判断标准】

1. 如果证据直接涉及用户询问的主题、概念、技术或研究问题，判断为 true。
2. 如果证据只能回答用户问题的一部分，但确实包含相关信息，判断为 true。
3. 如果证据只有关键词表面重合，没有实质内容联系，判断为 false。
4. 如果用户询问的是与论文知识库无关的现实生活信息，而论文证据无法提供答案，判断为 false。
5. 不要因为检索系统返回了证据，就默认证据相关。
6. 不要根据你自己的常识判断相关性，只判断提供的论文证据能否支持回答用户的问题。

请只返回合法 JSON，不要添加其他文字，格式如下：

{{"is_relevant": true}}

或者：

{{"is_relevant": false}}
"""

    try:
        response = client.chat.completions.create(
            model=settings.llm_model_name,
            messages=[
                {
                    "role": "system",
                    "content": "你是严格的论文证据相关性判断器，只返回 JSON。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError("相关性判断模型未返回内容")

        result = json.loads(content)

        if not isinstance(result.get("is_relevant"), bool):
            raise ValueError("相关性判断结果格式不正确")

        return result["is_relevant"]

    except Exception as e:
        raise AppError(
            f"论文证据相关性判断失败: {str(e)}",
            code="LLM_RELEVANCE_CHECK_FAILED",
            status_code=500,
        )


def filter_relevant_contexts(
    query: str,
    contexts: list[dict],
) -> list[dict]:
    """
    对检索到的论文证据逐条进行相关性判断。
    只返回与用户问题具有实质关联的证据。
    使用一次 LLM 调用批量判断，保留原始证据对象。
    """
    settings = get_settings()
    client = get_llm_client()

    if not contexts:
        return []

    # 为每条证据编号，便于模型逐条判断
    evidence_parts = []

    for index, context in enumerate(contexts):
        context_text = build_context_text([context])
        evidence_parts.append(
            f"【证据 {index}】\n{context_text}"
        )

    evidence_text = "\n\n".join(evidence_parts)

    prompt = f"""
你是一名严格的学术论文证据相关性评估助手。

请判断每条论文证据是否与用户问题具有实质性的内容关联。

【用户问题】
{query}

【待判断证据】
{evidence_text}

【判断标准】

1. 证据必须能够直接支持回答用户问题的某个方面。
2. 仅有关键词、术语或研究领域相似，但实际讨论内容无关，必须排除。
3. 讨论同一技术、算法、研究目标或问题的证据，可以保留。
4. 仅仅属于同一篇论文、同一领域，不代表相关。
5. 如果证据只提供背景信息，且不能帮助回答当前问题，应排除。
6. 不得根据外部常识推断证据相关性，只依据证据本身判断。
7. 不确定时采取严格标准，排除该条证据。

【返回要求】

只返回合法 JSON，不要添加解释或 Markdown。

relevant_indices 是相关证据的编号，从 0 开始。
如果没有任何相关证据，返回空数组。

格式：
{{"relevant_indices": [0, 2]}}
"""

    try:
        response = client.chat.completions.create(
            model=settings.llm_model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是严格的学术论文证据筛选器。"
                        "必须逐条判断，只返回合法 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError("证据过滤模型未返回内容")

        result = json.loads(content)
        relevant_indices = result.get("relevant_indices")

        if not isinstance(relevant_indices, list):
            raise ValueError("相关证据编号格式错误")

        # 校验编号，防止模型返回不存在的编号或布尔值
        valid_indices = []

        for index in relevant_indices:
            if (
                isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(contexts)
                and index not in valid_indices
            ):
                valid_indices.append(index)

        # 按原始检索顺序返回，保留所有原始元数据
        valid_indices.sort()

        filtered_contexts = [
            contexts[index]
            for index in valid_indices
        ]

        print(
            f"[Evidence Filter] 原始证据：{len(contexts)} 条，"
            f"过滤后：{len(filtered_contexts)} 条，"
            f"保留编号：{valid_indices}"
        )

        return filtered_contexts

    except Exception as e:
        raise AppError(
            f"论文证据逐条相关性过滤失败：{str(e)}",
            code="LLM_EVIDENCE_FILTER_FAILED",
            status_code=500,
        )
