from typing import TypedDict, List, Dict
from langgraph.graph import StateGraph, END
from app.services.paper_service import search_chunks
from app.services.llm_service import get_llm_client, generate_rag_answer
from sqlalchemy.orm import Session

# 1. 定义状态
class AgentState(TypedDict):
    query: str
    intent: str
    contexts: List[Dict]
    answer: str
    steps: List[str]

# 2. 节点：意图识别
def analyze_intent(state: AgentState):
    client = get_llm_client()
    prompt = (
        "请判断用户的意图，只输出一个单词：\n"
        "- 如果用户是在询问知识、概念、事实、论文内容，输出：RETRIEVE\n"
        "- 如果用户只是打招呼（如你好、谢谢），输出：CHAT\n"
        f"用户问题：{state['query']}"
    )
    try:
        res = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        intent = res.choices[0].message.content.strip().upper()
        if "RETRIEVE" not in intent:
            intent = "CHAT"
    except:
        intent = "RETRIEVE"
    
    state["intent"] = intent
    state["steps"].append(f"🤖 意图识别完成：判定为 {intent} 意图")
    return state

# 3. 节点：检索（这里为了简化，把检索结果直接塞给 state）
def retrieve_tool(state: AgentState):
    state["steps"].append(f"🔍 正在检索知识库，关键词: {state['query']}")
    # 注意：真实检索在 API 路由里调用 search_chunks，然后传进来
    return state

# 4. 节点：生成回答
def generate_answer(state: AgentState):
    if not state.get("contexts"):
        state["answer"] = "根据当前知识库，未找到相关证据。"
        state["steps"].append("❌ 未检索到证据，拒绝回答")
        return state
    
    state["steps"].append(f"🧠 提取到 {len(state['contexts'])} 条证据，正在总结...")
    answer = generate_rag_answer(state["query"], state["contexts"])
    state["answer"] = answer
    state["steps"].append("✅ 回答生成完毕")
    return state

# 5. 构建图
workflow = StateGraph(AgentState)
workflow.add_node("analyze_intent", analyze_intent)
workflow.add_node("retrieve", retrieve_tool)
workflow.add_node("generate", generate_answer)

workflow.set_entry_point("analyze_intent")
workflow.add_conditional_edges(
    "analyze_intent",
    lambda x: "retrieve" if x["intent"] == "RETRIEVE" else "generate"
)
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", END)

agent_app = workflow.compile()