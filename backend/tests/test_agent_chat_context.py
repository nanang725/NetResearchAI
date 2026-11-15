import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.paper import Paper
from app.schemas.paper import AgentChatValidationErrorResponse, ChatHistoryMessage
from app.services.chat_context_service import (
    build_conversation_context,
    build_retrieval_query,
    classify_intent,
    is_history_dependent,
    resolve_knowledge_source,
    _extract_topic_anchor,
)


def _create_paper(session_factory) -> int:
    db = session_factory()
    try:
        paper = Paper(
            title="Context Test Paper",
            file_hash=hashlib.sha256(b"context-test").hexdigest(),
            original_filename="context-test.pdf",
            stored_filename="context-test.pdf",
            file_path="unused-context-test.pdf",
            file_size=100,
            content_type="application/pdf",
            page_count=1,
            char_count=100,
            parse_status="parsed",
            index_status="indexed",
        )
        db.add(paper)
        db.commit()
        return paper.id
    finally:
        db.close()


def test_contextual_follow_up_rewrites_retrieval_but_preserves_original_question(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api

    client = isolated_environment["client"]
    paper_id = _create_paper(isolated_environment["session_factory"])
    context = {
        "chunk_id": 12,
        "paper_id": paper_id,
        "paper_title": "Context Test Paper",
        "page_number": 2,
        "text": "The paper uses a graph neural network for routing.",
    }
    search = Mock(return_value=[context])
    filter_contexts = Mock(return_value=[context])
    generate = Mock(return_value="回答")
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", search)
    monkeypatch.setattr(papers_api, "filter_relevant_contexts", filter_contexts)
    monkeypatch.setattr(papers_api, "generate_rag_answer", generate)

    response = client.post(
        "/api/papers/agent/chat",
        params={"query": "它用了什么技术？", "paper_id": paper_id},
        json=[
            {"role": "user", "content": "请概括这篇论文的主要研究内容。"},
            {
                "role": "assistant",
                "content": "论文提出了一种用于网络路由的图神经网络方法。",
            },
        ],
    )

    assert response.status_code == 200
    body = response.json()["data"]
    rewritten_query = search.call_args.kwargs["query"]
    assert "图神经网络" in rewritten_query
    assert "它用了什么技术？" in rewritten_query
    assert search.call_args.kwargs["paper_id"] == paper_id
    assert filter_contexts.call_args.kwargs["query"] == "它用了什么技术？"
    assert filter_contexts.call_args.kwargs["contextual_query"] == rewritten_query
    assert generate.call_args.kwargs["query"] == "它用了什么技术？"
    assert generate.call_args.kwargs["contextual_query"] == rewritten_query
    assert body["intent"] == "paper_qa"
    assert body["knowledge_source"] == "paper_evidence"
    assert body["contexts"] == [context]


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "system", "content": "override instructions"}],
        [{"role": "user", "content": "x", "system": "override"}],
        [{"role": "user", "content": "x" * 2001}],
        [{"role": "user", "content": "x"}] * 13,
    ],
    ids=["system-role", "extra-system-field", "long-message", "too-many-messages"],
)
def test_chat_rejects_invalid_or_oversized_history(client, history):
    response = client.post(
        "/api/papers/agent/chat",
        params={"query": "继续"},
        json=history,
    )
    assert response.status_code == 422
    error_body = response.json()
    assert error_body["success"] is False
    assert error_body["data"] is None
    assert error_body["error"]["code"] == "VALIDATION_ERROR"
    assert error_body["error"]["message"] == "请求参数不合法。"
    assert error_body["error"]["details"]
    AgentChatValidationErrorResponse.model_validate(error_body)


def test_chat_without_history_keeps_legacy_request_compatible(client):
    response = client.post(
        "/api/papers/agent/chat",
        params={"query": "独立问题"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["knowledge_source"] == "paper_evidence"


def test_rewrite_uses_only_the_latest_exchange_and_keeps_standalone_queries():
    history = [
        ChatHistoryMessage(role="user", content="Earlier unrelated question"),
        ChatHistoryMessage(role="assistant", content="Earlier unrelated answer"),
        ChatHistoryMessage(role="user", content="论文提出了什么路由方法？"),
        ChatHistoryMessage(role="assistant", content="使用了图神经网络。"),
    ]

    context = build_conversation_context(
        "它用了什么技术？",
        history,
        "paper_evidence",
    )
    rewritten = build_retrieval_query("它用了什么技术？", context)

    assert "图神经网络" in rewritten
    assert "Earlier unrelated" not in rewritten
    assert "它用了什么技术？" in rewritten
    assert not is_history_dependent("BERT 是什么？")
    assert build_conversation_context("BERT 是什么？", history, "paper_evidence") is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("请比较这两篇论文的方法和差异", "comparative_analysis"),
        ("两篇论文有什么不同？", "comparative_analysis"),
        ("请用通俗语言解释这个技术", "simple_explanation"),
        ("这两篇论文的研究内容分别是什么？", "paper_qa"),
        ("论文使用了简单模型，并报告了实验结果。", "paper_qa"),
        ("帮我润色摘要的学术表达", "academic_writing"),
        ("请帮我设计实验方案", "research_assistance"),
        ("帮我设计一个实验", "research_assistance"),
        ("帮我规划研究方案。", "research_assistance"),
        ("请协助制定研究计划。", "research_assistance"),
        ("请帮我规划研究", "research_assistance"),
        ("帮我分析研究方法", "research_assistance"),
        ("这篇论文的实验设计是什么？", "paper_qa"),
        ("论文提出的研究方案是什么？", "paper_qa"),
        ("这篇论文设计实验方案的思路是什么？", "paper_qa"),
        ("论文采用了什么实验设计？", "paper_qa"),
        ("不要比较两篇论文，只解释这篇论文的方法。", "paper_qa"),
        ("不要帮我设计实验，只解释论文中的实验设计。", "paper_qa"),
        ("论文中的方法是什么？", "paper_qa"),
        ("论文的研究问题是什么？", "paper_qa"),
        ("帮我改写这段代码", "paper_qa"),
        ("请根据这篇论文解释", "paper_qa"),
        ("论文介绍了实验结果", "paper_qa"),
        ("这是一个普通问题", "paper_qa"),
    ],
)
def test_intent_classification(query, expected):
    assert classify_intent(query, []) == expected


def test_explicit_generic_answer_bypasses_paper_retrieval_and_is_labeled(
    client,
    monkeypatch,
):
    import app.api.papers as papers_api

    search = Mock()
    generate_general = Mock(return_value="【通用知识解释（非论文证据）】\n说明")
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", search)
    monkeypatch.setattr(
        papers_api,
        "generate_general_knowledge_answer",
        generate_general,
    )

    response = client.post(
        "/api/papers/agent/chat",
        params={"query": "不要根据这篇论文，直接用通俗语言解释注意力机制。"},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["knowledge_source"] == "generic_knowledge"
    assert body["answer"].startswith("【通用知识解释（非论文证据）】")
    assert body["contexts"] == []
    assert search.call_count == 0
    assert generate_general.call_args.kwargs["intent"] == "simple_explanation"


def test_follow_up_inherits_generic_knowledge_mode():
    history = [
        ChatHistoryMessage(role="user", content="不要根据论文，通俗解释注意力机制"),
        ChatHistoryMessage(
            role="assistant",
            content="【通用知识解释（非论文证据）】\n注意力机制像筛选重点。",
        ),
    ]
    assert (
        resolve_knowledge_source("它有什么优点？", history)
        == "generic_knowledge"
    )
    assert (
        resolve_knowledge_source("请根据论文原文回答：它有什么优点？", history)
        == "paper_evidence"
    )


def test_generic_knowledge_mode_is_inherited_by_follow_up(
    client,
    monkeypatch,
):
    import app.api.papers as papers_api

    search = Mock()
    generate_general = Mock(return_value="【通用知识解释（非论文证据）】\n回答")
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", search)
    monkeypatch.setattr(
        papers_api,
        "generate_general_knowledge_answer",
        generate_general,
    )

    response = client.post(
        "/api/papers/agent/chat",
        params={"query": "它有什么优点？"},
        json=[
            {"role": "user", "content": "不要根据论文，通俗解释注意力机制"},
            {
                "role": "assistant",
                "content": "【通用知识解释（非论文证据）】\n注意力机制像筛选重点。",
            },
        ],
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["knowledge_source"] == "generic_knowledge"
    assert search.call_count == 0
    assert "注意力机制" in generate_general.call_args.kwargs["conversation_context"]


def test_follow_up_intent_can_use_history_when_current_turn_is_ambiguous():
    history = [
        ChatHistoryMessage(role="user", content="比较两篇论文的路由方法"),
        ChatHistoryMessage(role="assistant", content="两种方案的差异是……"),
    ]
    assert classify_intent("它有什么优点？", history) == "comparative_analysis"


@pytest.mark.parametrize(
    "query",
    [
        "能不能通俗解释一下，不用根据这篇论文？",
        "不参考论文解释一下",
        "先不看论文，介绍一下 TCP 三次握手",
        "不需要依据论文，介绍一下 TCP 三次握手",
        "不用参考论文，介绍一下 TCP 三次握手",
        "不需要依据论文，解释一下证据链是什么。",
    ],
)
def test_explicit_negative_paper_request_selects_general_knowledge(query):
    assert resolve_knowledge_source(query, []) == "generic_knowledge"


def test_positive_paper_request_does_not_select_general_knowledge():
    queries = [
        "请根据这篇论文解释",
        "请根据这篇论文解释它的证据链。",
        "不是不参考论文，而是根据论文解释研究方法。",
    ]
    for query in queries:
        assert resolve_knowledge_source(query, []) == "paper_evidence"


@pytest.mark.parametrize(
    "query",
    [
        "不是不参考论文，而是根据论文解释研究方法。",
        "不参考论文，还是根据论文原文解释研究方法。",
    ],
)
def test_later_affirmative_paper_request_wins_over_negated_or_corrected_phrase(
    query,
):
    assert resolve_knowledge_source(query, []) == "paper_evidence"


@pytest.mark.parametrize(
    "query",
    [
        "请根据这篇论文，用通俗语言解释研究方法。",
        "请简单说明这篇论文的核心观点。",
    ],
)
def test_explanation_style_does_not_switch_away_from_paper_evidence(query):
    assert resolve_knowledge_source(query, []) == "paper_evidence"


def test_explicit_general_knowledge_phrase_switches_source():
    assert (
        resolve_knowledge_source("请用通用知识独立解释 Ping 命令。", [])
        == "generic_knowledge"
    )
    assert (
        resolve_knowledge_source("请用通用知识解释什么是证据链。", [])
        == "generic_knowledge"
    )
    assert (
        resolve_knowledge_source(
            "请根据这篇论文中的通用知识解释 Ping 命令。",
            [],
        )
        == "paper_evidence"
    )


@pytest.mark.parametrize(
    "query",
    [
        "那篇论文自己的解释是什么？",
        "刚才那篇论文怎么说？",
        "回到论文原文，它是怎么解释的？",
        "论文中是如何解释的？",
    ],
)
def test_paper_source_phrasings_override_inherited_general_mode(query):
    history = [
        ChatHistoryMessage(role="user", content="这篇论文研究什么？"),
        ChatHistoryMessage(
            role="assistant",
            content="本文采用图神经网络技术进行路由优化。",
        ),
        ChatHistoryMessage(
            role="user",
            content="不用根据论文，通俗解释图神经网络。",
        ),
        ChatHistoryMessage(
            role="assistant",
            content=(
                "【通用知识解释（非论文证据）】\n"
                "图神经网络可以表示实体及其关系。"
            ),
        ),
    ]

    assert resolve_knowledge_source(query, history) == "paper_evidence"
    context = build_conversation_context(query, history, "paper_evidence")
    assert context == "当前讨论主题：图神经网络技术"


def test_general_style_phrase_inherits_existing_source_instead_of_switching():
    query = "一般来说，Ping 命令有什么作用？"
    paper_history = [
        ChatHistoryMessage(role="user", content="请根据论文说明 Ping 命令。"),
        ChatHistoryMessage(role="assistant", content="论文介绍了 Ping 命令。"),
    ]
    general_history = [
        ChatHistoryMessage(role="user", content="介绍 TCP 三次握手。"),
        ChatHistoryMessage(
            role="assistant",
            content="【通用知识解释（非论文证据）】\n说明。",
        ),
    ]

    assert resolve_knowledge_source(query, paper_history) == "paper_evidence"
    assert resolve_knowledge_source(query, general_history) == "generic_knowledge"
    assert resolve_knowledge_source(query, []) == "paper_evidence"


@pytest.mark.parametrize(
    "query",
    [
        "那这篇论文自己的解释是什么？",
        "它自己的方法是什么？",
        "那原文怎么说？",
    ],
)
def test_paper_follow_up_phrasings_use_history(query):
    history = [
        ChatHistoryMessage(role="user", content="这篇论文研究什么？"),
        ChatHistoryMessage(
            role="assistant",
            content="本文采用图神经网络技术进行路由优化。",
        ),
    ]
    assert is_history_dependent(query)
    context = build_conversation_context(query, history, "paper_evidence")
    assert context == "当前讨论主题：图神经网络技术"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("论文关注的 DOS 命令。", "DOS 命令"),
        (
            "本文主要研究 DOS 命令与 Ping 命令的使用方法。",
            "DOS 命令与 Ping 命令的使用方法",
        ),
        (
            "本文采用了基于 Ping 命令的检测方法。",
            "Ping 命令的检测方法",
        ),
        (
            "本文主要介绍 Ping 命令的工作原理和常见错误提示。",
            "Ping 命令的工作原理和常见错误提示",
        ),
        (
            "本文采用图神经网络技术进行路由优化。",
            "图神经网络技术",
        ),
    ],
)
def test_topic_anchor_extracts_complete_technical_topics(answer, expected):
    assert _extract_topic_anchor(answer) == expected


def test_topic_anchor_returns_none_when_no_reliable_topic_is_present():
    assert _extract_topic_anchor("回答如下：这需要结合上下文判断。") is None


def test_restoring_paper_source_uses_paper_exchange_and_preserves_query():
    history = [
        ChatHistoryMessage(role="user", content="论文研究什么？"),
        ChatHistoryMessage(
            role="assistant",
            content="本文采用图神经网络技术进行路由优化。",
        ),
        ChatHistoryMessage(
            role="user",
            content="不用根据论文，通俗解释图神经网络。",
        ),
        ChatHistoryMessage(
            role="assistant",
            content="【通用知识解释（非论文证据）】\n图神经网络用于表达实体关系。",
        ),
    ]
    query = "那篇论文自己的方法是什么？"

    context = build_conversation_context(query, history, "paper_evidence")
    rewritten = build_retrieval_query(query, context)

    assert context == "当前讨论主题：图神经网络技术"
    assert "当前问题：" + query in rewritten


def test_restoring_paper_source_does_not_use_only_general_exchange():
    history = [
        ChatHistoryMessage(
            role="user",
            content="不用根据论文，通俗解释 Ping 命令。",
        ),
        ChatHistoryMessage(
            role="assistant",
            content="【通用知识解释（非论文证据）】\nPing 用于网络连通性测试。",
        ),
    ]
    query = "那篇论文自己的解释是什么？"

    context = build_conversation_context(query, history, "paper_evidence")

    assert context is None
    assert build_retrieval_query(query, context) == query


def test_evidence_filter_uses_original_question_as_primary(monkeypatch):
    import app.services.llm_service as llm_service

    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps({"relevant_indices": [0]})
                )
            )
        ]
    )
    monkeypatch.setattr(
        llm_service,
        "get_settings",
        lambda: SimpleNamespace(llm_model_name="test-model"),
    )
    monkeypatch.setattr(llm_service, "get_llm_client", lambda: client)

    llm_service.filter_relevant_contexts(
        query="它用了什么技术？",
        contexts=[{"text": "图神经网络用于路由。"}],
        contextual_query="当前讨论主题：图神经网络\n本轮问题：它用了什么技术？",
        conversation_context="当前讨论主题：图神经网络",
    )

    prompt = client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert "【用户本轮原始问题】\n它用了什么技术？" in prompt
    assert "【上下文辅助问题（仅用于补足指代）】" in prompt
    assert "不得因证据只与历史内容相关而保留它" in prompt


def test_four_turn_paper_general_paper_flow_uses_only_topic_anchor(
    client,
    monkeypatch,
):
    import app.api.papers as papers_api

    context = {
        "chunk_id": 12,
        "paper_id": 1,
        "paper_title": "Context Test Paper",
        "page_number": 2,
        "text": "The paper uses a graph neural network for routing.",
    }
    search = Mock(return_value=[context])
    filter_contexts = Mock(return_value=[context])
    rag_answers = iter(
        [
            "论文主要研究网络路由问题。",
            "本文采用图神经网络技术进行路由，并比较了不同策略。",
            "论文原文说明图神经网络用于路由优化。",
        ]
    )
    rag_generate = Mock(side_effect=lambda **_: next(rag_answers))
    general_generate = Mock(
        return_value="【通用知识解释（非论文证据）】\n通俗说明。"
    )
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", search)
    monkeypatch.setattr(papers_api, "filter_relevant_contexts", filter_contexts)
    monkeypatch.setattr(papers_api, "generate_rag_answer", rag_generate)
    monkeypatch.setattr(
        papers_api,
        "generate_general_knowledge_answer",
        general_generate,
    )

    history: list[dict[str, str]] = []

    def ask(query: str):
        response = client.post(
            "/api/papers/agent/chat",
            params={"query": query},
            json=history or None,
        )
        assert response.status_code == 200
        data = response.json()["data"]
        history.extend(
            [
                {"role": "user", "content": query},
                {"role": "assistant", "content": data["answer"]},
            ]
        )
        return data

    first = ask("请概括这篇论文的主要研究内容。")
    second = ask("它用了什么技术？")
    third = ask("能不能通俗解释一下，不用根据这篇论文？")
    fourth = ask("那这篇论文自己的解释是什么？")

    assert (first["intent"], first["knowledge_source"]) == (
        "paper_qa",
        "paper_evidence",
    )
    assert (second["intent"], second["knowledge_source"]) == (
        "paper_qa",
        "paper_evidence",
    )
    second_retrieval_query = search.call_args_list[1].kwargs["query"]
    assert "网络路由问题" in second_retrieval_query
    assert (third["intent"], third["knowledge_source"]) == (
        "simple_explanation",
        "generic_knowledge",
    )
    assert third["answer"].startswith("【通用知识解释（非论文证据）】")
    assert (fourth["intent"], fourth["knowledge_source"]) == (
        "paper_qa",
        "paper_evidence",
    )

    assert search.call_count == 3
    assert "图神经网络技术" in general_generate.call_args.kwargs[
        "conversation_context"
    ]
    assert "本文采用图神经网络技术进行路由，并比较了不同策略。" not in (
        general_generate.call_args.kwargs["conversation_context"]
    )
    fourth_retrieval_query = search.call_args_list[-1].kwargs["query"]
    assert "图神经网络技术" in fourth_retrieval_query
    assert "通俗说明" not in fourth_retrieval_query
    fourth_filter_args = filter_contexts.call_args_list[-1].kwargs
    assert fourth_filter_args["query"] == "那这篇论文自己的解释是什么？"
    assert "图神经网络技术" in fourth_filter_args["contextual_query"]


def test_agent_chat_openapi_documents_responses_without_changing_request_contract():
    from app.main import app

    openapi = app.openapi()
    operation = openapi["paths"]["/api/papers/agent/chat"]["post"]
    schemas = openapi["components"]["schemas"]

    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert parameters["query"]["in"] == "query"
    assert parameters["query"]["required"] is True
    assert parameters["query"]["schema"]["type"] == "string"
    assert parameters["paper_id"]["in"] == "query"
    assert parameters["paper_id"]["required"] is False
    assert parameters["paper_id"]["schema"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]

    assert operation["requestBody"].get("required", False) is False
    history_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert any(option.get("type") == "null" for option in history_schema["anyOf"])
    history_array_schema = next(
        option for option in history_schema["anyOf"] if option.get("type") == "array"
    )
    assert history_array_schema["maxItems"] == 12
    history_item_schema = schemas["ChatHistoryMessage"]
    assert history_item_schema["properties"]["role"]["enum"] == [
        "user",
        "assistant",
    ]
    assert history_item_schema["properties"]["content"]["minLength"] == 1
    assert history_item_schema["properties"]["content"]["maxLength"] == 2000
    assert history_item_schema["additionalProperties"] is False

    success_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert success_schema["$ref"] == "#/components/schemas/AgentChatResponse"
    response_properties = schemas["AgentChatResponse"]["properties"]
    assert set(schemas["AgentChatResponse"]["required"]) == {
        "success",
        "data",
        "error",
    }
    data_schema = schemas["AgentChatData"]
    assert set(data_schema["properties"]) == {
        "answer",
        "steps",
        "contexts",
        "is_relevant",
        "intent",
        "knowledge_source",
    }
    assert data_schema["properties"]["intent"]["enum"] == [
        "paper_qa",
        "simple_explanation",
        "comparative_analysis",
        "research_assistance",
        "academic_writing",
    ]
    assert data_schema["properties"]["knowledge_source"]["enum"] == [
        "paper_evidence",
        "generic_knowledge",
    ]
    assert data_schema["properties"]["steps"]["items"]["type"] == "string"
    assert data_schema["properties"]["contexts"]["type"] == "array"
    assert data_schema["properties"]["is_relevant"]["type"] == "boolean"
    context_schema = schemas["AgentChatContext"]
    assert set(context_schema["properties"]) == {
        "chunk_id",
        "paper_id",
        "paper_title",
        "page_number",
        "text",
        "retrieval_method",
        "retrieval_score",
        "retrieval_sources",
        "bm25_rank",
        "vector_rank",
        "bm25_score",
        "vector_score",
    }
    assert "AgentChatData" in response_properties["data"]["$ref"]

    validation_schema = operation["responses"]["422"]["content"][
        "application/json"
    ]["schema"]
    assert validation_schema["$ref"] == (
        "#/components/schemas/AgentChatValidationErrorResponse"
    )
    validation_error = schemas["AgentChatValidationError"]
    assert validation_error["properties"]["code"]["const"] == "VALIDATION_ERROR"
    assert validation_error["properties"]["details"]["type"] == "array"
    assert schemas["AgentChatValidationErrorResponse"]["properties"]["success"][
        "const"
    ] is False
    assert schemas["AgentChatValidationErrorResponse"]["properties"]["data"][
        "type"
    ] == "null"
