from openai import OpenAI
from app.config import get_settings  # 正确导入 get_settings 函数
from app.core.exceptions import AppError

def get_llm_client():
    settings = get_settings()  # 在这里调用获取配置
    if not settings.llm_api_key:
        raise AppError("未配置 LLM_API_KEY，请检查 .env 文件", code="LLM_NOT_CONFIGURED", status_code=500)
    return OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

def generate_rag_answer(query: str, contexts: list[dict]) -> str:
    settings = get_settings()  # 在这里调用获取配置
    client = get_llm_client()
    
    context_text = ""
    for i, c in enumerate(contexts):
        context_text += f"[{i+1}] 论文: {c['paper_title']} (第 {c['page_number']} 页)\n原文: {c['text']}\n\n"
        
    prompt = f"""你是一个严谨的计算机网络科研助手。
请严格根据以下提供的真实文献证据来回答用户的问题。
如果证据中找不到答案，请明确回答“根据当前知识库，未找到相关证据”，绝对不允许编造。

用户问题：{query}

参考文献证据：
{context_text}

请给出你的回答，并在引用证据时标注序号，如 [1]、[2]。"""

    try:
        response = client.chat.completions.create(
            model=settings.llm_model_name,
            messages=[
                {"role": "system", "content": "你是一个严谨的学术助手，只根据提供的文献证据回答。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content
    except Exception as e:
        raise AppError(f"大模型调用失败: {str(e)}", code="LLM_ERROR", status_code=500)