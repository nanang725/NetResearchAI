from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database import Base
from app.models.paper import Paper, PaperElement, PaperPage, PaperVersion
from app.services import citation_service


@pytest.fixture()
def citation_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'citations.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    try:
        yield factory
    finally:
        engine.dispose()


def _add_paper(
    db,
    title: str,
    *,
    doi: str | None = None,
    year: int | None = None,
    authors: str | None = "Alice Smith, Bob Jones",
    arxiv_id: str | None = None,
) -> Paper:
    digest = hashlib.sha256(f"{title}-{uuid4()}".encode()).hexdigest()
    paper = Paper(
        title=title,
        file_hash=digest,
        original_filename=f"{arxiv_id or title}.pdf",
        stored_filename=f"{uuid4()}.pdf",
        file_path=f"unused/{digest}.pdf",
        file_size=1,
        content_type="application/pdf",
        page_count=2,
        char_count=100,
        authors=authors,
        doi=doi,
        normalized_title=citation_service.normalize_title(title),
        publication_year=year,
        parse_status="parsed",
        parse_checkpoint_version=1,
        parse_source_hash=digest,
        parse_source_size=1,
        parse_source_page_count=2,
        index_status="indexed",
    )
    db.add(paper)
    db.flush()
    return paper


def _add_reference_element(
    db,
    paper: Paper,
    text: str,
    *,
    page_number: int = 2,
    element_index: int = 0,
) -> PaperElement:
    page = (
        db.query(PaperPage)
        .filter(
            PaperPage.paper_id == paper.id,
            PaperPage.page_number == page_number,
        )
        .first()
    )
    if page is None:
        page = PaperPage(
            paper_id=paper.id,
            page_number=page_number,
            text=text,
            char_count=len(text),
            parse_status="text_extracted",
            checkpoint_status="completed",
        )
        db.add(page)
        db.flush()
    element = PaperElement(
        paper_id=paper.id,
        page_id=page.id,
        page_number=page_number,
        element_index=element_index,
        element_type="reference",
        text=text,
        extraction_status="heuristic",
    )
    db.add(element)
    db.flush()
    return element


def test_reference_normalization_and_field_extraction():
    assert citation_service.normalize_doi(
        "https://doi.org/10.1109/ACCESS.2024.123"
    ) == "10.1109/access.2024.123"
    assert citation_service.normalize_arxiv_id("arXiv:2402.00881v3") == "2402.00881"
    assert citation_service.normalize_arxiv_id("hep-th/9901001") == "hep-th/9901001"
    assert citation_service.normalize_title("  A  Survey: RAG! ") == "asurveyrag"

    entries = citation_service.split_reference_entries(
        "[1] Alice. Reliable Routing. 2024. doi:10.1000/ABC\n"
        "[2] Bob. Future Networks. 2023."
    )
    assert len(entries) == 2
    fields = citation_service.extract_reference_fields(entries[0])
    assert fields["doi"] == "10.1000/abc"
    assert fields["year"] == 2024
    assert "Reliable Routing" in fields["title"]


def test_exact_doi_reference_creates_one_traceable_edge_and_is_idempotent(
    citation_session_factory,
):
    with citation_session_factory() as db:
        source = _add_paper(db, "Citing Paper")
        target = _add_paper(
            db,
            "Reliable Packet Routing",
            doi="10.1000/ROUTING",
            year=2024,
        )
        element = _add_reference_element(
            db,
            source,
            "[1] Alice Smith. A slightly different title. 2024. "
            "doi:10.1000/routing\n"
            "[2] Alice Smith. Reliable Packet Routing. 2024. "
            "doi:10.1000/routing",
        )
        db.commit()

        first = citation_service.rebuild_citations(db, [source.id])
        second = citation_service.rebuild_citations(db, [source.id])
        references = db.query(citation_service.CitationReference).all()
        edges = db.query(citation_service.CitationEdge).all()

        assert first["edge_count"] == second["edge_count"] == 1
        assert len(references) == 2
        assert len(edges) == 1
        assert all(reference.target_paper_id == target.id for reference in references)
        assert all(reference.source_element_id == element.id for reference in references)
        assert all(reference.match_status == "exact" for reference in references)
        assert all(reference.match_method == "doi" for reference in references)
        assert edges[0].citing_paper_id == source.id
        assert edges[0].cited_paper_id == target.id
        assert edges[0].citation_reference_id in {
            reference.id for reference in references
        }


def test_ambiguous_title_match_is_preserved_without_edge(
    citation_session_factory,
):
    with citation_session_factory() as db:
        source = _add_paper(db, "Ambiguous Source")
        _add_paper(db, "Shared Network Survey", year=2024)
        _add_paper(db, "Shared Network Survey", year=2024)
        _add_reference_element(
            db,
            source,
            "Alice Smith. Shared Network Survey. 2024.",
        )
        db.commit()
        stats = citation_service.rebuild_citations(db, [source.id])
        reference = db.query(citation_service.CitationReference).one()

        assert reference.match_status == "ambiguous"
        assert reference.target_paper_id is None
        assert stats["ambiguous_reference_count"] == 1
        assert stats["edge_count"] == 0


def test_unmatched_reference_rematches_when_new_paper_is_added(
    citation_session_factory,
):
    with citation_session_factory() as db:
        source = _add_paper(db, "Unmatched Source")
        _add_reference_element(
            db,
            source,
            "Alice Smith. Future Directions for Edge Networks. 2025.",
        )
        db.commit()
        initial = citation_service.rebuild_citations(db, [source.id])
        assert initial["unmatched_reference_count"] == 1
        assert initial["edge_count"] == 0

        target = _add_paper(
            db,
            "Future Directions for Edge Networks",
            year=2025,
            authors="Alice Smith",
        )
        db.commit()
        updated = citation_service.rebuild_citations(db, [target.id])
        reference = db.query(citation_service.CitationReference).one()

        assert reference.target_paper_id == target.id
        assert reference.match_status in {"exact", "high_confidence"}
        assert updated["edge_count"] == 1


def test_self_reference_does_not_create_edge(
    citation_session_factory,
):
    with citation_session_factory() as db:
        source = _add_paper(
            db,
            "Self Referencing Paper",
            doi="10.1000/self",
        )
        _add_reference_element(
            db,
            source,
            "Alice Smith. Self Referencing Paper. 2024. doi:10.1000/self",
        )
        db.commit()
        stats = citation_service.rebuild_citations(db, [source.id])
        reference = db.query(citation_service.CitationReference).one()

        assert reference.match_status == "self_reference"
        assert stats["edge_count"] == 0
        assert stats["unmatched_reference_count"] == 1


def test_versions_are_collapsed_to_one_graph_target(
    citation_session_factory,
):
    with citation_session_factory() as db:
        source = _add_paper(db, "Version Citing Source")
        version_one = _add_paper(db, "Versioned Network Method", year=2023)
        version_two = _add_paper(db, "Versioned Network Method", year=2024)
        db.add_all(
            [
                PaperVersion(
                    group_id="version-group",
                    paper_id=version_one.id,
                    version_number=1,
                    retained=True,
                ),
                PaperVersion(
                    group_id="version-group",
                    paper_id=version_two.id,
                    version_number=2,
                    retained=False,
                ),
            ]
        )
        _add_reference_element(
            db,
            source,
            "Alice Smith. Versioned Network Method. 2024.",
        )
        db.commit()
        citation_service.rebuild_citations(db, [source.id])
        graph = citation_service.get_citation_graph(db)
        edge = db.query(citation_service.CitationEdge).one()

        assert edge.cited_paper_id == version_one.id
        assert edge.cited_paper_id != version_two.id
        assert graph["stats"]["independent_paper_count"] == 2


def test_deleting_target_removes_edge_and_reference_is_reported_unmatched(
    citation_session_factory,
):
    with citation_session_factory() as db:
        source = _add_paper(db, "Delete Source")
        target = _add_paper(
            db,
            "Temporary Target",
            doi="10.1000/delete",
        )
        _add_reference_element(
            db,
            source,
            "Alice Smith. Temporary Target. 2024. doi:10.1000/delete",
        )
        db.commit()
        citation_service.rebuild_citations(db, [source.id])
        assert db.query(citation_service.CitationEdge).count() == 1

        db.delete(target)
        db.commit()
        reference = db.query(citation_service.CitationReference).one()
        stats = citation_service.citation_statistics(db)

        assert reference.target_paper_id is None
        assert db.query(citation_service.CitationEdge).count() == 0
        assert stats["matched_reference_count"] == 0
        assert stats["unmatched_reference_count"] == 1


def test_pagerank_direction_dangling_nodes_and_stability():
    edges = [(1, 2), (3, 2), (2, 4), (4, 2)]
    first = citation_service.compute_pagerank([1, 2, 3, 4], edges)
    second = citation_service.compute_pagerank([1, 2, 3, 4], edges)

    assert first == second
    assert first[2] > first[4] > first[1]
    assert first[2] > first[3]
    assert citation_service.compute_pagerank([], []) == {}
    assert citation_service.compute_pagerank([9], []) == {9: pytest.approx(1.0)}


def test_twenty_paper_virtual_graph_builds_with_real_graph_algorithms(
    citation_session_factory,
):
    with citation_session_factory() as db:
        papers = [
            _add_paper(
                db,
                f"Graph Paper {index:02d}",
                year=2000 + index,
            )
            for index in range(1, 21)
        ]
        for index, source in enumerate(papers):
            targets = [
                papers[target_index]
                for target_index in (index + 1, index + 2)
                if target_index < len(papers)
            ]
            reference_text = "\n".join(
                f"[{entry_index}] Alice Smith. {target.title}. {target.publication_year}."
                for entry_index, target in enumerate(targets, start=1)
            )
            _add_reference_element(db, source, reference_text)
        db.commit()

        stats = citation_service.rebuild_citations(db)
        graph = citation_service.get_citation_graph(db, limit=200)

        assert stats["reference_count"] == 37
        assert stats["edge_count"] == 37
        assert graph["stats"]["paper_count"] == 20
        assert len(graph["nodes"]) == 20
        assert len(graph["edges"]) == 37
        assert graph["stats"]["foundational_paper_count"] >= 1
        assert all(node["pagerank"] > 0 for node in graph["nodes"])
        assert max(node["pagerank"] for node in graph["nodes"]) > (
            1 / 20
        )
