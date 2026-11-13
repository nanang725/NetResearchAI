
from app.database import SessionLocal
from app.models.paper import Paper
from app.services.embedding_service import (
    index_paper,
    get_vector_index_stats,
)


def main():
    db = SessionLocal()

    try:
        papers = db.query(Paper).order_by(Paper.id).all()

        print(f"发现 {len(papers)} 篇论文，开始建立向量索引。")

        for paper in papers:
            print(
                f"\n正在处理论文 ID={paper.id}：{paper.title}"
            )

            try:
                success = index_paper(
                    db=db,
                    paper_id=paper.id,
                    force=False,
                )

                if success:
                    print(f"论文 {paper.id} 索引成功。")
                else:
                    print(f"论文 {paper.id} 索引失败。")

            except Exception as exc:
                db.rollback()
                print(f"论文 {paper.id} 处理异常：{exc}")

        print("\n========== 索引统计 ==========")
        print(get_vector_index_stats())

        print("\n========== 论文索引状态 ==========")

        db.expire_all()

        for paper in db.query(Paper).order_by(Paper.id).all():
            print(
                f"ID={paper.id} | "
                f"标题={paper.title} | "
                f"状态={paper.index_status}"
            )

    finally:
        db.close()


if __name__ == "__main__":
    main()
