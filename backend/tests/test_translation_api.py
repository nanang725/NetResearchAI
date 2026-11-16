from __future__ import annotations

import copy
import hashlib
import re
from types import SimpleNamespace

import pytest

from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.services import embedding_service, translation_service


class FakeCompletions:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def create(self, **kwargs):
        if self.fail:
            raise TimeoutError("simulated translation timeout")
        self.calls.append(kwargs)
        user_prompt = kwargs["messages"][-1]["content"]
        match = re.search(
            r"【待翻译原文】\n(.*?)\n\n【翻译要求】",
            user_prompt,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("Translation prompt is missing protected source text")
        protected_text = match.group(1)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f"Translated academic text: {protected_text}"
                    )
                )
            ]
        )


class FakeLLM:
    def __init__(self, *, fail: bool = False):
        self.chat = SimpleNamespace(completions=FakeCompletions(fail=fail))


class InvalidResponseLLM:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: SimpleNamespace(choices=[]),
            )
        )


class FakeCollection:
    def __init__(self):
        self.records: dict[str, dict] = {}

    def get(self, ids=None, where=None, include=None, **_kwargs):
        include = include or []
        selected = [
            vector_id
            for vector_id, record in self.records.items()
            if (
                (ids is not None and vector_id in ids)
                or (
                    ids is None
                    and (
                        where is None
                        or all(
                            record["metadata"].get(key) == value
                            for key, value in where.items()
                        )
                    )
                )
            )
        ]
        result = {"ids": selected}
        if "documents" in include:
            result["documents"] = [
                self.records[vector_id]["document"] for vector_id in selected
            ]
        if "metadatas" in include:
            result["metadatas"] = [
                copy.deepcopy(self.records[vector_id]["metadata"])
                for vector_id in selected
            ]
        if "embeddings" in include:
            result["embeddings"] = [
                list(self.records[vector_id]["embedding"]) for vector_id in selected
            ]
        return result

    def upsert(self, ids, embeddings, documents, metadatas):
        for vector_id, embedding, document, metadata in zip(
            ids,
            embeddings,
            documents,
            metadatas,
        ):
            self.records[vector_id] = {
                "embedding": list(embedding),
                "document": document,
                "metadata": copy.deepcopy(metadata),
            }

    def delete(self, ids=None, where=None):
        if ids is not None:
            for vector_id in ids:
                self.records.pop(vector_id, None)
            return
        if where is not None:
            for vector_id, record in list(self.records.items()):
                if all(
                    record["metadata"].get(key) == value
                    for key, value in where.items()
                ):
                    self.records.pop(vector_id, None)

    def count(self):
        return len(self.records)


def _create_paper(
    session_factory,
    *,
    title: str,
    page_text: str,
    element_text: str | None = None,
) -> tuple[int, int, int, int]:
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()
    with session_factory() as db:
        paper = Paper(
            title=title,
            file_hash=digest,
            original_filename=f"{title}.pdf",
            stored_filename=f"{digest}.pdf",
            file_path=f"unused/{digest}.pdf",
            file_size=max(len(page_text.encode("utf-8")), 1),
            content_type="application/pdf",
            page_count=1,
            char_count=len(page_text),
            authors="Test Author",
            pdf_metadata="{}",
            parse_status="parsed",
            parse_checkpoint_version=1,
            parse_source_hash=digest,
            parse_source_size=max(len(page_text.encode("utf-8")), 1),
            parse_source_page_count=1,
            index_status="pending",
        )
        db.add(paper)
        db.flush()
        page = PaperPage(
            paper_id=paper.id,
            page_number=1,
            text=page_text,
            char_count=len(page_text),
            parse_status="text_extracted",
            checkpoint_status="completed",
        )
        db.add(page)
        db.flush()
        element = PaperElement(
            paper_id=paper.id,
            page_id=page.id,
            page_number=1,
            element_index=0,
            element_type="paragraph",
            text=element_text if element_text is not None else page_text,
            extraction_status="extracted",
        )
        db.add(element)
        db.flush()
        chunk = PaperChunk(
            paper_id=paper.id,
            page_number=1,
            page_chunk_index=0,
            chunk_index=0,
            text=page_text,
            content_type="paragraph",
            element_id=element.id,
        )
        db.add(chunk)
        db.commit()
        return paper.id, page.id, element.id, chunk.id


def _database_snapshot(session_factory, paper_id: int) -> dict:
    with session_factory() as db:
        paper = db.query(Paper).filter(Paper.id == paper_id).one()
        pages = (
            db.query(PaperPage)
            .filter(PaperPage.paper_id == paper_id)
            .order_by(PaperPage.id)
            .all()
        )
        elements = (
            db.query(PaperElement)
            .filter(PaperElement.paper_id == paper_id)
            .order_by(PaperElement.id)
            .all()
        )
        chunks = (
            db.query(PaperChunk)
            .filter(PaperChunk.paper_id == paper_id)
            .order_by(PaperChunk.id)
            .all()
        )
        return {
            "paper": (
                paper.id,
                paper.title,
                paper.page_count,
                paper.char_count,
                paper.parse_status,
                paper.index_status,
            ),
            "pages": [
                (page.id, page.page_number, page.text, page.checkpoint_status)
                for page in pages
            ],
            "elements": [
                (
                    element.id,
                    element.page_id,
                    element.element_index,
                    element.element_type,
                    element.text,
                )
                for element in elements
            ],
            "chunks": [
                (
                    chunk.id,
                    chunk.page_number,
                    chunk.page_chunk_index,
                    chunk.chunk_index,
                    chunk.text,
                    chunk.content_type,
                    chunk.element_id,
                )
                for chunk in chunks
            ],
        }


def _translation_post(
    client,
    paper_id: int,
    *,
    page_number: int = 1,
    direction: str = "zh_to_en",
    element_id: int | None = None,
    extra: dict | None = None,
):
    payload = {
        "page_number": page_number,
        "direction": direction,
        **({"element_id": element_id} if element_id is not None else {}),
        **(extra or {}),
    }
    return client.post(
        f"/api/papers/{paper_id}/translate",
        json=payload,
    )


def test_element_translation_preserves_source_and_index_data(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    source_text = (
        "Transformer 方法使用 TCP 与公式 $E=mc^2$，准确率 95.5%，"
        "引用 [1,2]，DOI 10.1000/xyz，网址 https://example.com/paper。"
    )
    paper_id, _page_id, element_id, chunk_id = _create_paper(
        factory,
        title="Translation Preservation",
        page_text=source_text,
        element_text=source_text,
    )
    collection = FakeCollection()
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        embedding_service,
        "encode_texts",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    with factory() as db:
        embedding_service.index_paper(db, paper_id)
        paper = db.query(Paper).filter(Paper.id == paper_id).one()
        chunk = db.query(PaperChunk).filter(PaperChunk.id == chunk_id).one()
        record_before = embedding_service.build_chunk_index_record(chunk, paper)
    database_before = _database_snapshot(factory, paper_id)
    chroma_before = copy.deepcopy(collection.records)

    fake_llm = FakeLLM()
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: fake_llm,
    )

    response = _translation_post(
        isolated_environment["client"],
        paper_id,
        direction="zh_to_en",
        element_id=element_id,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["scope"] == "element"
    assert data["element_id"] == element_id
    assert data["direction"] == "zh_to_en"
    assert data["source_language"] == "中文"
    assert data["target_language"] == "英文"
    assert data["original_text"] == source_text
    assert data["protected_token_count"] >= 5
    assert "不是论文原文" in data["disclaimer"]
    assert "TCP" in data["translated_text"]
    assert "$E=mc^2$" in data["translated_text"]
    assert "95.5%" in data["translated_text"]
    assert "[1,2]" in data["translated_text"]
    assert "10.1000/xyz" in data["translated_text"]
    assert "https://example.com/paper" in data["translated_text"]
    assert "中文学术文本翻译为英文" in fake_llm.chat.completions.calls[0]["messages"][-1]["content"]

    assert _database_snapshot(factory, paper_id) == database_before
    assert collection.records == chroma_before
    with factory() as db:
        paper = db.query(Paper).filter(Paper.id == paper_id).one()
        chunk = db.query(PaperChunk).filter(PaperChunk.id == chunk_id).one()
        assert embedding_service.build_chunk_index_record(chunk, paper) == record_before


def test_whole_page_en_to_zh_uses_database_text(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    source_text = "A reliable routing protocol evaluates packet delivery [3]."
    paper_id, _page_id, _element_id, _chunk_id = _create_paper(
        factory,
        title="English Translation",
        page_text=source_text,
    )
    fake_llm = FakeLLM()
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: fake_llm,
    )

    response = _translation_post(
        isolated_environment["client"],
        paper_id,
        direction="en_to_zh",
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scope"] == "page"
    assert data["element_id"] is None
    assert data["source_language"] == "英文"
    assert data["target_language"] == "中文"
    assert data["original_text"] == source_text
    assert "英文学术文本翻译为中文" in fake_llm.chat.completions.calls[0]["messages"][-1]["content"]


def test_translation_ownership_and_validation_errors(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    first_paper_id, _first_page_id, first_element_id, _ = _create_paper(
        factory,
        title="First Paper",
        page_text="First paper paragraph [1].",
    )
    second_paper_id, _second_page_id, second_element_id, _ = _create_paper(
        factory,
        title="Second Paper",
        page_text="Second paper paragraph [2].",
    )
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: FakeLLM(),
    )
    client = isolated_environment["client"]

    missing_paper = _translation_post(client, 999999)
    assert missing_paper.status_code == 404
    assert missing_paper.json()["error"]["code"] == "PAPER_NOT_FOUND"

    missing_page = _translation_post(
        client,
        first_paper_id,
        page_number=99,
    )
    assert missing_page.status_code == 404
    assert missing_page.json()["error"]["code"] == "PAGE_NOT_FOUND"

    missing_element = _translation_post(
        client,
        first_paper_id,
        element_id=999999,
    )
    assert missing_element.status_code == 404
    assert missing_element.json()["error"]["code"] == "ELEMENT_NOT_FOUND"

    cross_paper = _translation_post(
        client,
        first_paper_id,
        element_id=second_element_id,
    )
    assert cross_paper.status_code == 404
    assert cross_paper.json()["error"]["code"] == "ELEMENT_NOT_FOUND"

    same_paper_other_element = _translation_post(
        client,
        first_paper_id,
        element_id=first_element_id,
        page_number=2,
    )
    assert same_paper_other_element.status_code == 404
    assert same_paper_other_element.json()["error"]["code"] == "PAGE_NOT_FOUND"

    forbidden_override = _translation_post(
        client,
        first_paper_id,
        extra={"original_text": "client supplied text"},
    )
    assert forbidden_override.status_code == 422

    # The second paper remains accessible with its own element.
    valid_second = _translation_post(
        client,
        second_paper_id,
        element_id=second_element_id,
    )
    assert valid_second.status_code == 200


def test_translation_failure_is_recoverable_and_does_not_modify_database(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    paper_id, _page_id, element_id, _chunk_id = _create_paper(
        factory,
        title="Translation Failure",
        page_text="数据库原文不能被修改 [4]。",
    )
    before = _database_snapshot(factory, paper_id)
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: FakeLLM(fail=True),
    )

    response = _translation_post(
        isolated_environment["client"],
        paper_id,
        element_id=element_id,
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "TRANSLATION_TIMEOUT"
    assert _database_snapshot(factory, paper_id) == before


def test_translation_rejects_malformed_provider_response(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    paper_id, *_ = _create_paper(
        factory,
        title="Malformed Translation",
        page_text="A valid source paragraph [5].",
    )
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: InvalidResponseLLM(),
    )

    response = _translation_post(
        isolated_environment["client"],
        paper_id,
        direction="en_to_zh",
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "TRANSLATION_INVALID_RESPONSE"


def test_translation_rejects_overlong_page_text(
    isolated_environment,
    monkeypatch,
):
    factory = isolated_environment["session_factory"]
    paper_id, *_ = _create_paper(
        factory,
        title="Long Translation Page",
        page_text="A" * (translation_service.MAX_TRANSLATION_CHARS + 1),
    )
    client = FakeLLM()
    monkeypatch.setattr(
        translation_service,
        "get_llm_client",
        lambda: client,
    )
    response = _translation_post(
        isolated_environment["client"],
        paper_id,
        direction="en_to_zh",
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "TRANSLATION_TEXT_TOO_LONG"
    assert client.chat.completions.calls == []


def test_sensitive_literals_are_placeholder_protected_and_restored():
    original = (
        "Transformer 方法使用 TCP 与 $E=mc^2$，准确率 95.5%，"
        "引用 [1,2]，DOI 10.1000/xyz，网址 https://example.com/paper，"
        "机构 New York University。"
    )
    protected = translation_service.protect_sensitive_text(original)

    for literal in (
        "TCP",
        "$E=mc^2$",
        "95.5%",
        "[1,2]",
        "10.1000/xyz",
        "https://example.com/paper",
        "New York University",
    ):
        assert literal not in protected.text
    assert protected.tokens

    translated = f"Translated: {protected.text}"
    restored = translation_service.restore_sensitive_text(
        translated,
        protected.tokens,
    )
    for _placeholder, literal in protected.tokens:
        assert literal in restored

    with pytest.raises(AppError) as error:
        translation_service.restore_sensitive_text(
            translated.replace(protected.tokens[0][0], ""),
            protected.tokens,
        )
    assert error.value.code == "TRANSLATION_PROTECTION_FAILED"
