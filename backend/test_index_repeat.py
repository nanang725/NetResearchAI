
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
            title="临时重复索引测试论文",
            file_hash=unique_id,
            original_filename="test_repeat.pdf",
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
                text=f"这是重复索引测试的第 {i + 1} 个文本块。",
            )
            for i in range(3)
        ]

        db.add_all(chunks)
        db.commit()

        def mock_encode_texts(texts):
            return [[1.0] + [0.0] * 1023 for _ in texts]

        es.encode_texts = mock_encode_texts

        collection = es.get_chroma_collection()

        print(f"临时论文 ID：{paper.id}")
        print(f"测试前向量总数：{original_count}")

        # 第一次索引
        result1 = es.index_paper(db, paper.id, force=False)
        count1 = len(collection.get(
            where={"paper_id": paper.id},
            include=[],
        )["ids"])

        print(f"\n第一次索引：{result1['status']}")
        print(f"第一次索引后本篇向量数：{count1}")

        # 第二次索引
        result2 = es.index_paper(db, paper.id, force=False)
        count2 = len(collection.get(
            where={"paper_id": paper.id},
            include=[],
        )["ids"])

        print(f"\n第二次索引：{result2['status']}")
        print(f"第二次索引后本篇向量数：{count2}")
        print(f"当前向量库总数：{collection.count()}")

        assert result1["status"] == "indexed"
        assert result2["status"] == "indexed"
        assert count1 == 3
        assert count2 == 3
        assert collection.count() == original_count + 3

        print("\n测试通过：重复索引没有产生重复向量！")

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
