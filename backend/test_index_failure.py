
from datetime import datetime
from uuid import uuid4

from app.database import SessionLocal
from app.models.paper import Paper, PaperChunk
from app.services import embedding_service as es


def main():
    db = SessionLocal()
    paper = None

    original_encode = es.encode_texts
    original_count = es.get_chroma_collection().count()

    call_count = 0

    def mock_encode_texts(texts):
        nonlocal call_count
        call_count += 1

        if call_count == 2:
            raise RuntimeError("模拟第二批向量编码失败")

        # 模拟 1024 维 BGE-M3 向量
        return [
            [1.0] + [0.0] * 1023
            for _ in texts
        ]

    try:
        # 1. 创建临时论文
        now = datetime.utcnow()
        unique_id = uuid4().hex

        paper = Paper(
            title="临时异常测试论文",
            file_hash=unique_id,
            original_filename="test_failure.pdf",
            stored_filename=f"{unique_id}.pdf",
            file_path=f"test/{unique_id}.pdf",
            file_size=1024,
            content_type="application/pdf",
            page_count=101,
            char_count=10100,
            parse_status="success",
            index_status="pending",
            created_at=now,
            updated_at=now,
        )

        db.add(paper)
        db.commit()
        db.refresh(paper)

        # 2. 创建 101 个临时文本块
        chunks = [
            PaperChunk(
                paper_id=paper.id,
                page_number=i + 1,
                chunk_index=0,
                text=f"这是用于异常恢复测试的临时文本块，第 {i + 1} 条。",
            )
            for i in range(101)
        ]

        db.add_all(chunks)
        db.commit()

        print(f"临时论文 ID：{paper.id}")
        print("临时文本块数量：101")
        print(f"测试前向量总数：{original_count}")

        # 3. 替换编码函数，模拟第二批失败
        es.encode_texts = mock_encode_texts

        try:
            es.index_paper(
                db=db,
                paper_id=paper.id,
                force=False,
            )
            print("错误：没有触发预期异常！")

        except RuntimeError as exc:
            print(f"成功触发模拟异常：{exc}")

        # 4. 检查异常恢复结果
        db.refresh(paper)

        collection = es.get_chroma_collection()

        remaining = collection.get(
            where={"paper_id": paper.id},
            include=[],
        )

        print("\n========== 异常恢复检查 ==========")
        print(f"论文是否保留：{paper.id is not None}")
        print(f"论文索引状态：{paper.index_status}")
        print(f"临时论文残留向量：{len(remaining['ids'])}")
        print(f"当前向量总数：{collection.count()}")
        print(f"编码函数调用次数：{call_count}")

        assert paper.index_status == "failed"
        assert len(remaining["ids"]) == 0
        assert collection.count() == original_count
        assert call_count == 2

        print("\n测试通过：异常恢复符合预期！")

    finally:
        # 5. 恢复原编码函数
        es.encode_texts = original_encode

        # 6. 清理临时向量和数据库记录
        if paper is not None and paper.id is not None:
            collection = es.get_chroma_collection()

            collection.delete(
                where={"paper_id": paper.id}
            )

            db.query(PaperChunk).filter(
                PaperChunk.paper_id == paper.id
            ).delete(synchronize_session=False)

            db.query(Paper).filter(
                Paper.id == paper.id
            ).delete(synchronize_session=False)

            db.commit()

            print("\n临时测试数据已清理。")
            print(f"清理后向量总数：{collection.count()}")

        db.close()


if __name__ == "__main__":
    main()
