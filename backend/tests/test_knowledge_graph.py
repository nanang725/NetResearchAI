from __future__ import annotations

from app.models.citation import CitationEdge, CitationReference
from app.models.knowledge_graph import KnowledgeEdge, KnowledgeNode
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.schemas.research_agent import AgentBudgetRequest
from app.services.knowledge_graph_service import (
    compare_methods,
    explore_research_gaps,
    find_relation_path,
    get_core_papers,
    get_knowledge_graph,
)
from app.services.research_agent.planner import fallback_plan
from app.services.research_agent.tools import TOOL_REGISTRY, ToolContext


def _create_paper(
    db,
    *,
    title: str,
    file_hash: str,
    authors: str,
    text: str,
):
    paper = Paper(
        title=title,
        file_hash=file_hash,
        original_filename=f"{file_hash}.pdf",
        stored_filename=f"{file_hash}.pdf",
        file_path=f"/test/{file_hash}.pdf",
        file_size=1024,
        authors=authors,
        publication_year=2024,
        page_count=1,
        char_count=len(text),
        parse_status="parsed",
        index_status="indexed",
    )
    db.add(paper)
    db.flush()
    page = PaperPage(
        paper_id=paper.id,
        page_number=1,
        text=text,
        char_count=len(text),
    )
    db.add(page)
    db.flush()
    element = PaperElement(
        paper_id=paper.id,
        page_id=page.id,
        page_number=1,
        element_index=0,
        element_type="paragraph",
        text=text,
        section_path="1 Introduction",
    )
    chunk = PaperChunk(
        paper_id=paper.id,
        page_number=1,
        chunk_index=0,
        page_chunk_index=0,
        text=text,
        content_type="paragraph",
    )
    db.add_all([element, chunk])
    db.flush()
    return paper, page, element, chunk


def _seed_graph_corpus(session_factory):
    db = session_factory()
    try:
        source, _page, _element, source_chunk = _create_paper(
            db,
            title="Evidence Graph for Academic Question Answering",
            file_hash="graph-source",
            authors="Alice Example and Bob Researcher",
            text=(
                "Alice Example and Bob Researcher\n"
                "Department of Computer Science, Example University\n"
                "Retrieval-Augmented Generation combines large language models "
                "and information retrieval. We evaluate the graph neural network "
                "method on the SQuAD dataset with a BERT model. Results show "
                "that hybrid retrieval improves academic question answering."
            ),
        )
        target, _target_page, _target_element, target_chunk = _create_paper(
            db,
            title="Prior Retrieval Systems",
            file_hash="graph-target",
            authors="Carol Prior",
            text=(
                "Carol Prior, Institute for Retrieval Research\n"
                "BM25 is a sparse retrieval method for information retrieval. "
                "Results show that BM25 remains a strong baseline."
            ),
        )
        reference_page = PaperPage(
            paper_id=source.id,
            page_number=4,
            text=(
                "[1] Carol Prior. Prior Retrieval Systems. 2024. "
                "We extend the retrieval method [1] with graph evidence. "
                "Evidence from local papers supports the extended comparison. "
                "The prior baseline contradicts [1] the reported improvement."
            ),
            char_count=180,
        )
        db.add(reference_page)
        db.flush()
        reference_element = PaperElement(
            paper_id=source.id,
            page_id=reference_page.id,
            page_number=4,
            element_index=0,
            element_type="reference",
            text=reference_page.text,
            section_path="References",
        )
        reference_chunk = PaperChunk(
            paper_id=source.id,
            page_number=4,
            chunk_index=4,
            page_chunk_index=0,
            text=reference_page.text,
            content_type="reference",
            element_id=None,
        )
        db.add_all([reference_element, reference_chunk])
        db.flush()
        reference_chunk.element_id = reference_element.id
        reference = CitationReference(
            source_paper_id=source.id,
            source_page_id=reference_page.id,
            source_element_id=reference_element.id,
            source_page_number=4,
            source_entry_index=0,
            raw_text="[1] Carol Prior. Prior Retrieval Systems. 2024.",
            normalized_text="carol prior prior retrieval systems 2024",
            title=target.title,
            normalized_title="priorretrievalsystems",
            authors=target.authors,
            year=2024,
            target_paper_id=target.id,
            match_status="matched",
            match_method="title",
            match_confidence=0.95,
        )
        db.add(reference)
        db.flush()
        db.add(
            CitationEdge(
                citing_paper_id=source.id,
                cited_paper_id=target.id,
                citation_reference_id=reference.id,
                match_method="title",
                match_confidence=0.95,
            )
        )
        db.commit()
        return {
            "source_id": source.id,
            "target_id": target.id,
            "source_chunk_id": source_chunk.id,
            "target_chunk_id": target_chunk.id,
            "reference_chunk_id": reference_chunk.id,
        }
    finally:
        db.close()


def test_knowledge_graph_rebuild_extracts_grounded_entities_and_relations(
    isolated_environment,
):
    client = isolated_environment["client"]
    paper_ids = _seed_graph_corpus(
        isolated_environment["session_factory"]
    )

    rebuilt = client.post("/api/knowledge-graph/rebuild", json={})
    assert rebuilt.status_code == 200, rebuilt.text
    result = rebuilt.json()["data"]
    assert result["status"] == "completed"
    assert result["paper_count"] == 2
    assert result["node_count"] >= 10
    assert result["edge_count"] >= 10

    graph_response = client.get("/api/knowledge-graph")
    assert graph_response.status_code == 200, graph_response.text
    graph = graph_response.json()["data"]
    node_types = {node["node_type"] for node in graph["nodes"]}
    assert {
        "paper",
        "author",
        "topic",
        "method",
        "dataset",
        "model",
        "finding",
        "institution",
    }.issubset(node_types)
    relation_types = {edge["relation_type"] for edge in graph["edges"]}
    assert {
        "CITES",
        "AUTHORED_BY",
        "STUDIES",
        "USES_METHOD",
        "USES_DATASET",
        "USES_MODEL",
        "SUPPORTS",
        "CONTRADICTS",
        "EXTENDS",
    }.issubset(relation_types)
    assert all(
        edge["paper_id"]
        and edge["page_number"]
        and edge["chunk_id"]
        and edge["evidence_text"]
        for edge in graph["edges"]
    )

    cited = next(
        edge for edge in graph["edges"] if edge["relation_type"] == "CITES"
    )
    assert cited["chunk_id"] == paper_ids["reference_chunk_id"]
    assert cited["page_number"] == 4
    extended = next(
        edge for edge in graph["edges"] if edge["relation_type"] == "EXTENDS"
    )
    assert "[1]" in extended["evidence_text"]


def test_knowledge_graph_analysis_is_data_driven(
    isolated_environment,
):
    client = isolated_environment["client"]
    ids = _seed_graph_corpus(isolated_environment["session_factory"])
    response = client.post("/api/knowledge-graph/rebuild", json={})
    assert response.status_code == 200, response.text

    core = client.get(
        "/api/knowledge-graph/analysis/core-papers",
        params={"limit": 2},
    )
    assert core.status_code == 200, core.text
    assert core.json()["data"]["result_count"] >= 1

    methods = client.get(
        "/api/knowledge-graph/analysis/method-comparison",
        params=[
            ("paper_ids", ids["source_id"]),
            ("paper_ids", ids["target_id"]),
        ],
    )
    assert methods.status_code == 200, methods.text
    assert methods.json()["data"]["methods"]
    assert all(
        method["evidence"]
        for method in methods.json()["data"]["methods"]
    )

    filtered = client.get(
        "/api/knowledge-graph",
        params=[
            ("node_types", "topic"),
            ("relation_types", "STUDIES"),
        ],
    )
    assert filtered.status_code == 200, filtered.text
    assert all(
        node["node_type"] in {"paper", "topic"}
        for node in filtered.json()["data"]["nodes"]
    )
    assert all(
        edge["relation_type"] == "STUDIES"
        for edge in filtered.json()["data"]["edges"]
    )

    path = client.get(
        "/api/knowledge-graph/path",
        params={
            "source_paper_id": ids["source_id"],
            "target_paper_id": ids["target_id"],
            "max_depth": 4,
        },
    )
    assert path.status_code == 200, path.text
    assert path.json()["data"]["path_found"] is True
    assert path.json()["data"]["depth"] >= 1

    gaps = client.get(
        "/api/knowledge-graph/research-gaps",
        params={"topic": "Information Retrieval"},
    )
    assert gaps.status_code == 200, gaps.text
    assert any(
        item["gap_type"] in {
            "single_paper_topic",
            "limited_topic_coverage",
            "method_without_cross_paper_comparison",
        }
        for item in gaps.json()["data"]["gaps"]
    )


def test_knowledge_graph_service_returns_empty_graph_before_rebuild(
    isolated_environment,
):
    db = isolated_environment["session_factory"]()
    try:
        graph = get_knowledge_graph(db)
        assert graph["nodes"] == []
        assert graph["edges"] == []
        assert graph["stats"]["missing_provenance_edge_count"] == 0
        assert get_core_papers(db)["papers"] == []
        assert explore_research_gaps(db)["gaps"] == []
    finally:
        db.close()


def test_agent_knowledge_graph_tools_preserve_local_evidence(
    isolated_environment,
):
    ids = _seed_graph_corpus(isolated_environment["session_factory"])
    db = isolated_environment["session_factory"]()
    try:
        from app.services.knowledge_extraction_service import (
            rebuild_knowledge_graph,
        )

        rebuild_knowledge_graph(db)
        context = ToolContext(
            db=db,
            run_id="knowledge-test-run",
            node_id="knowledge-test-node",
            cancel_check=lambda: False,
        )
        explored = TOOL_REGISTRY.execute(
            "explore_knowledge_graph",
            {
                "paper_id": ids["source_id"],
                "query": "",
                "limit": 100,
            },
            context,
        )
        assert explored["success"] is True
        assert explored["sources"]
        assert all(
            source["paper_id"]
            and source["page_number"]
            and source["chunk_id"]
            and source["excerpt"]
            for source in explored["sources"]
        )

        compared = TOOL_REGISTRY.execute(
            "compare_knowledge_methods",
            {"paper_ids": [ids["source_id"], ids["target_id"]]},
            context,
        )
        assert compared["success"] is True
        assert compared["result"]["methods"]
        assert compared["sources"]

        path = TOOL_REGISTRY.execute(
            "find_knowledge_path",
            {
                "source_paper_id": ids["source_id"],
                "target_paper_id": ids["target_id"],
                "max_depth": 4,
            },
            context,
        )
        assert path["success"] is True
        assert path["result"]["path_found"] is True
        assert path["sources"]

        gaps = TOOL_REGISTRY.execute(
            "explore_research_gaps",
            {"topic": "Information Retrieval", "limit": 20},
            context,
        )
        assert gaps["success"] is True
        assert gaps["result"]["gaps"]
        assert gaps["sources"]
    finally:
        db.close()

    plan = fallback_plan(
        "比较知识图谱中的方法与研究空白",
        AgentBudgetRequest(),
    )
    assert "inspect_knowledge_graph" in {node.id for node in plan.nodes}
