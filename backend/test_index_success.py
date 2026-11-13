
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

    try:
        now = datetime.utcnow()
        unique_id = uuid4().hex

        paper = Paper(
            title="临时正常索引测试论文",
            file_hash=unique_id,
            original_filename="test_success.pdf",
            stored_filename=f"{unique_id}.pdf",
            file_path=f"test/{unique_id}.pdf",
            file_size=1024,
            content_type="application/pdf",
            page_count=3,
            char_count=300,
            parse_status="success",
            index_status="pending",
            created_at=now,
            updated_at=now,
        )

        db.add(paper)
        db.commit()
        db.refresh(paper)

        chunks = [
            PaperChunk(
                paper_id=paper.id,
                page_number=i + 1,
                chunk_index=0,
                text=f"这是正常索引测试的第 {i + 1} 个文本块。",
            )
            for i in range(3)
        ]

        db.add_all(chunks)
        db.commit()

        # 模拟 BGE-M3 返回 1024 维向量，避免重新加载模型
        def mock_encode_texts(texts):
            return [
                [1.0] + [0.0] * 1023
                for _ in texts
            ]

        es.encode_texts = mock_encode_texts

        print(f"临时论文 ID：{paper.id}")
        print("待索引文本块：3")
        print(f"测试前向量总数：{original_count}")

        result = es.index_paper(
            db=db,
            paper_id=paper.id,
            force=False,
        )

        db.refresh(paper)

        collection = es.get_chroma_collection()
        indexed_vectors = collection.get(
            where={"paper_id": paper.id},
            include=[],
        )

        print("\n========== 正常索引检查 ==========")
        print(f"索引返回状态：{result.get('status')}")
        print(f"论文索引状态：{paper.index_status}")
        print(f"本篇论文向量数量：{len(indexed_vectors['ids'])}")
        print(f"当前向量总数：{collection.count()}")

        assert result.get("status") == "indexed"
        assert paper.index_status == "indexed"
        assert len(indexed_vectors["ids"]) == 3
        assert collection.count() == original_count + 3

        print("\n测试通过：正常索引完整写入！")

    finally:
        es.encode_texts = original_encode

        if paper is not None and paper.id is not None:
            collection = es.get_chroma_collection()
            collection.delete(where={"paper_id": paper.id})

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
