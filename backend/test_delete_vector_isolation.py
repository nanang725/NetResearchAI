
from datetime import datetime
from uuid import uuid4

from app.database import SessionLocal
from app.models.paper import Paper, PaperChunk
from app.services import embedding_service as es


def create_test_paper(db, title, unique_id):
    now = datetime.utcnow()

    paper = Paper(
        title=title,
        file_hash=unique_id,
        original_filename=f"{unique_id}.pdf",
        stored_filename=f"{unique_id}.pdf",
        file_path=f"test/{unique_id}.pdf",
        file_size=1024,
        content_type="application/pdf",
        page_count=2,
        char_count=200,
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
            text=f"{title}，这是第 {i + 1} 个测试文本块。",
        )
        for i in range(2)
    ]

    db.add_all(chunks)
    db.commit()

    return paper


def get_vector_count(collection, paper_id):
    result = collection.get(
        where={"paper_id": paper_id},
        include=[],
    )
    return len(result["ids"])


def main():
    db = SessionLocal()
    paper_a = None
    paper_b = None
    original_encode = es.encode_texts
    original_count = es.get_chroma_collection().count()

    try:
        es.encode_texts = lambda texts: [
            [1.0] + [0.0] * 1023 for _ in texts
        ]

        paper_a = create_test_paper(
            db, "临时删除测试论文A", uuid4().hex
        )
        paper_b = create_test_paper(
            db, "临时删除测试论文B", uuid4().hex
        )

        collection = es.get_chroma_collection()

        print(f"论文 A ID：{paper_a.id}")
        print(f"论文 B ID：{paper_b.id}")
        print(f"测试前向量总数：{original_count}")

        # 分别建立两篇论文的向量索引
        result_a = es.index_paper(db, paper_a.id)
        result_b = es.index_paper(db, paper_b.id)

        count_a_before = get_vector_count(collection, paper_a.id)
        count_b_before = get_vector_count(collection, paper_b.id)

        print("\n========== 删除前检查 ==========")
        print(f"论文 A 索引状态：{result_a['status']}")
        print(f"论文 B 索引状态：{result_b['status']}")
        print(f"论文 A 向量数：{count_a_before}")
        print(f"论文 B 向量数：{count_b_before}")
        print(f"当前向量总数：{collection.count()}")

        assert count_a_before == 2
        assert count_b_before == 2
        assert collection.count() == original_count + 4

        # 只删除论文 A 的向量
        es.delete_paper_vectors(paper_a.id)

        count_a_after = get_vector_count(collection, paper_a.id)
        count_b_after = get_vector_count(collection, paper_b.id)

        print("\n========== 删除后检查 ==========")
        print(f"论文 A 剩余向量数：{count_a_after}")
        print(f"论文 B 剩余向量数：{count_b_after}")
        print(f"当前向量总数：{collection.count()}")

        assert count_a_after == 0
        assert count_b_after == 2
        assert collection.count() == original_count + 2

        print("\n测试通过：删除论文 A 的向量没有影响论文 B！")

    finally:
        es.encode_texts = original_encode

        collection = es.get_chroma_collection()

        for paper in (paper_a, paper_b):
            if paper is not None and paper.id is not None:
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
