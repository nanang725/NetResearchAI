import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.paper import Paper
from app.services import llm_service


def _create_paper(session_factory) -> int:
    db = session_factory()
    try:
        paper = Paper(
            title="Enumeration Test Paper",
            file_hash=hashlib.sha256(b"enumeration-test").hexdigest(),
            original_filename="enumeration-test.pdf",
            stored_filename="enumeration-test.pdf",
            file_path="unused-enumeration-test.pdf",
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


@pytest.mark.parametrize(
    ("query", "single_paper", "expected_limit"),
    [
        (
            "请列出这篇论文中 Ping 命令的常见错误提示及其含义。",
            True,
            8,
        ),
        ("解释这篇论文的研究主题。", True, 5),
        ("这篇论文中有哪些错误提示？", False, 5),
    ],
    ids=["single-paper-enumeration", "single-paper-ordinary", "library-enumeration"],
)
def test_agent_chat_uses_expected_retrieval_limit(
    isolated_environment,
    monkeypatch,
    query,
    single_paper,
    expected_limit,
):
    import app.api.papers as papers_api

    client = isolated_environment["client"]
    search = Mock(return_value=[])
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", search)

    params = {"query": query}
    if single_paper:
        params["paper_id"] = _create_paper(
            isolated_environment["session_factory"]
        )

    response = client.post("/api/papers/agent/chat", params=params)

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert search.call_args.kwargs["limit"] == expected_limit


@pytest.mark.parametrize(
    "query",
    [
        "列出常见问题",
        "列举主要方法",
        "有哪些错误提示",
        "列出全部类型",
        "列出所有原因",
        "常见类型是什么",
    ],
)
def test_enumeration_query_markers(query):
    assert llm_service.is_enumeration_query(query)


def _mock_filter_client(monkeypatch, relevant_indices):
    client = Mock()
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {"relevant_indices": relevant_indices}
                    )
                )
            )
        ]
    )
    client.chat.completions.create.return_value = response
    monkeypatch.setattr(
        llm_service,
        "get_settings",
        lambda: SimpleNamespace(llm_model_name="test-model"),
    )
    monkeypatch.setattr(llm_service, "get_llm_client", lambda: client)
    return client


def test_enumeration_filter_keeps_complementary_evidence(monkeypatch):
    client = _mock_filter_client(monkeypatch, [1, 3])
    contexts = [
        {"chunk_id": 141, "text": "本文介绍 Ping 命令的用途。"},
        {"chunk_id": 144, "text": "Request timed out：网卡安装或配置有问题。"},
        {"chunk_id": 148, "text": "网络测试时应确保网关配置正确。"},
        {
            "chunk_id": 146,
            "text": "Destination host Unreachable：目标主机不可达。",
        },
    ]

    filtered = llm_service.filter_relevant_contexts(
        "请列出 Ping 命令的常见错误提示及其含义。",
        contexts,
    )

    assert [item["chunk_id"] for item in filtered] == [144, 146]
    prompt = client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert "枚举问题补充判断" in prompt
    assert "互补关系" in prompt


def test_ordinary_filter_keeps_strict_background_exclusion(monkeypatch):
    client = _mock_filter_client(monkeypatch, [1])
    contexts = [
        {"chunk_id": 141, "text": "本文研究计算机网络，背景介绍如下。"},
        {"chunk_id": 144, "text": "Request timed out 表示请求超时。"},
    ]

    filtered = llm_service.filter_relevant_contexts(
        "Request timed out 表示什么？",
        contexts,
    )

    assert [item["chunk_id"] for item in filtered] == [144]
    prompt = client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert "枚举问题补充判断" not in prompt
    assert "仅有关键词、术语或研究领域相似" in prompt
