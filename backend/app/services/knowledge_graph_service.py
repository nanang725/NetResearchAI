from __future__ import annotations

import json
import math
from collections import Counter, defaultdict, deque
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.knowledge_graph import (
    KnowledgeEdge,
    KnowledgeExtractionRun,
    KnowledgeNode,
)
from app.models.paper import Paper
from app.services.knowledge_extraction_service import (
    NODE_TYPES,
    RELATION_TYPES,
    rebuild_knowledge_graph,
)


KNOWLEDGE_GRAPH_DISCLAIMER = (
    "图谱只展示当前本地论文中可定位到页码、Chunk 和证据文本的抽取结果。"
    "规则匹配不等于论文事实结论，也不代表完整学术领域知识。"
)
SEMANTIC_PAPER_RELATIONS = {"SUPPORTS", "CONTRADICTS", "EXTENDS"}
RELATION_WEIGHTS = {
    "CITES": 1.0,
    "EXTENDS": 1.2,
    "SUPPORTS": 0.8,
    "CONTRADICTS": 0.9,
    "STUDIES": 0.4,
    "USES_METHOD": 0.45,
    "USES_DATASET": 0.35,
    "USES_MODEL": 0.4,
    "AUTHORED_BY": 0.25,
    "AFFILIATED_WITH": 0.2,
}


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        result = json.loads(value)
        return result
    except (TypeError, json.JSONDecodeError):
        return default


def _node_dict(node: KnowledgeNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "node_type": node.node_type,
        "label": node.label,
        "normalized_name": node.normalized_name,
        "paper_id": node.paper_id,
        "attributes": _loads(node.attributes_json, {}),
    }


def _edge_dict(
    edge: KnowledgeEdge,
    *,
    source: KnowledgeNode,
    target: KnowledgeNode,
    paper: Paper | None,
) -> dict[str, Any]:
    return {
        "id": edge.id,
        "source": source.id,
        "target": target.id,
        "source_label": source.label,
        "target_label": target.label,
        "source_type": source.node_type,
        "target_type": target.node_type,
        "relation_type": edge.relation_type,
        "paper_id": edge.paper_id,
        "paper_title": paper.title if paper is not None else None,
        "page_number": edge.page_number,
        "chunk_id": edge.chunk_id,
        "evidence_text": edge.evidence_text,
        "confidence": edge.confidence,
        "extraction_method": edge.extraction_method,
        "attributes": _loads(edge.attributes_json, {}),
    }


def _weighted_pagerank(
    node_ids: Iterable[int],
    edges: Iterable[tuple[int, int, float]],
    *,
    damping: float = 0.85,
    iterations: int = 40,
) -> dict[int, float]:
    ids = sorted(set(int(node_id) for node_id in node_ids))
    if not ids:
        return {}
    outgoing: dict[int, list[tuple[int, float]]] = defaultdict(list)
    incoming_ids = set(ids)
    for source, target, weight in edges:
        if source in incoming_ids and target in incoming_ids and source != target:
            outgoing[source].append((target, max(0.0001, float(weight))))
    count = len(ids)
    ranks = {node_id: 1.0 / count for node_id in ids}
    for _ in range(iterations):
        next_ranks = {
            node_id: (1.0 - damping) / count
            for node_id in ids
        }
        dangling = 0.0
        for source in ids:
            targets = outgoing.get(source, [])
            if not targets:
                dangling += ranks[source]
                continue
            total_weight = sum(weight for _target, weight in targets)
            for target, weight in targets:
                next_ranks[target] += (
                    damping * ranks[source] * weight / total_weight
                )
        dangling_share = damping * dangling / count
        for node_id in ids:
            next_ranks[node_id] += dangling_share
        delta = sum(
            abs(next_ranks[node_id] - ranks[node_id])
            for node_id in ids
        )
        ranks = next_ranks
        if delta < 1e-10:
            break
    return ranks


def _paper_relation_metrics(
    nodes: dict[int, KnowledgeNode],
    edges: list[KnowledgeEdge],
) -> dict[int, dict[str, Any]]:
    paper_nodes = {
        node.id: node
        for node in nodes.values()
        if node.node_type == "paper" and node.paper_id is not None
    }
    paper_edges: list[tuple[int, int, float]] = []
    incoming = Counter()
    outgoing = Counter()
    projected_connections: dict[int, set[int]] = defaultdict(set)
    evidence_count = Counter(
        edge.source_node_id for edge in edges
    )
    for edge in edges:
        if (
            edge.source_node_id in paper_nodes
            and edge.target_node_id in paper_nodes
            and edge.relation_type
            in {"CITES", *SEMANTIC_PAPER_RELATIONS}
        ):
            weight = RELATION_WEIGHTS.get(edge.relation_type, 0.5)
            paper_edges.append(
                (edge.source_node_id, edge.target_node_id, weight)
            )
            outgoing[edge.source_node_id] += 1
            incoming[edge.target_node_id] += 1
    entity_to_papers: dict[int, set[int]] = defaultdict(set)
    for edge in edges:
        if edge.relation_type in {"CITES", *SEMANTIC_PAPER_RELATIONS}:
            continue
        if edge.source_node_id in paper_nodes:
            entity_to_papers[edge.target_node_id].add(edge.source_node_id)
        if edge.target_node_id in paper_nodes:
            entity_to_papers[edge.source_node_id].add(edge.target_node_id)
    projected_weights: Counter[frozenset[int]] = Counter()
    for paper_ids in entity_to_papers.values():
        ordered_ids = sorted(paper_ids)
        if len(ordered_ids) < 2:
            continue
        rarity_weight = 1.0 / len(ordered_ids)
        for left_index, left in enumerate(ordered_ids):
            for right in ordered_ids[left_index + 1:]:
                pair = frozenset((left, right))
                projected_weights[pair] += rarity_weight
    for pair, shared_weight in projected_weights.items():
        left, right = tuple(pair)
        weight = min(0.5, 0.02 + shared_weight * 0.06)
        paper_edges.append((left, right, weight))
        paper_edges.append((right, left, weight))
        projected_connections[left].add(right)
        projected_connections[right].add(left)
    ranks = _weighted_pagerank(paper_nodes, paper_edges)
    ordered = sorted(
        paper_nodes,
        key=lambda node_id: (
            ranks.get(node_id, 0.0),
            incoming[node_id],
            evidence_count[node_id],
        ),
        reverse=True,
    )
    rank_by_id = {
        node_id: index + 1 for index, node_id in enumerate(ordered)
    }
    core_count = (
        min(5, max(1, math.ceil(len(ordered) * 0.25)))
        if ordered and paper_edges
        else 0
    )
    core_ids = set(ordered[:core_count])
    return {
        node_id: {
            "paper_pagerank": round(ranks.get(node_id, 0.0), 10),
            "paper_rank": rank_by_id[node_id],
            "paper_in_degree": incoming[node_id],
            "paper_out_degree": outgoing[node_id],
            "paper_evidence_count": evidence_count[node_id],
            "paper_projected_connection_count": len(
                projected_connections[node_id]
            ),
            "is_core": node_id in core_ids,
        }
        for node_id in paper_nodes
    }


def _latest_run_dict(db: Session) -> dict[str, Any] | None:
    run = (
        db.query(KnowledgeExtractionRun)
        .order_by(
            KnowledgeExtractionRun.started_at.desc(),
            KnowledgeExtractionRun.id.desc(),
        )
        .first()
    )
    if run is None:
        return None
    return {
        "run_id": run.id,
        "status": run.status,
        "paper_count": run.paper_count,
        "node_count": run.node_count,
        "edge_count": run.edge_count,
        "skipped_chunk_count": run.skipped_chunk_count,
        "diagnostics": _loads(run.diagnostics_json, []),
        "error": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": (
            run.completed_at.isoformat() if run.completed_at else None
        ),
    }


def get_knowledge_graph(
    db: Session,
    *,
    node_types: Iterable[str] | None = None,
    relation_types: Iterable[str] | None = None,
    paper_id: int | None = None,
    query: str = "",
    limit: int = 300,
) -> dict[str, Any]:
    selected_node_types = set(node_types or NODE_TYPES)
    selected_relation_types = set(relation_types or RELATION_TYPES)
    invalid_types = selected_node_types - NODE_TYPES
    invalid_relations = selected_relation_types - RELATION_TYPES
    if invalid_types or invalid_relations:
        raise AppError(
            "知识图谱筛选参数包含未知类型。",
            code="KNOWLEDGE_GRAPH_INVALID_FILTER",
            status_code=422,
            details={
                "node_types": sorted(invalid_types),
                "relation_types": sorted(invalid_relations),
            },
        )
    selected_node_types.add("paper")
    if paper_id is not None and db.get(Paper, paper_id) is None:
        raise AppError(
            "论文不存在。",
            code="PAPER_NOT_FOUND",
            status_code=404,
        )

    relevant_paper_ids: set[int] = set()
    if paper_id is not None:
        relevant_paper_ids.add(paper_id)
    else:
        relevant_paper_ids.update(
            value for (value,) in db.query(Paper.id).all()
        )
    paper_nodes = (
        db.query(KnowledgeNode)
        .filter(
            KnowledgeNode.node_type == "paper",
            KnowledgeNode.paper_id.in_(relevant_paper_ids),
        )
        .all()
        if relevant_paper_ids
        else []
    )
    relevant_node_ids = {node.id for node in paper_nodes}
    edges_query = db.query(KnowledgeEdge).filter(
        KnowledgeEdge.relation_type.in_(selected_relation_types)
    )
    if paper_id is not None:
        edges_query = edges_query.filter(KnowledgeEdge.paper_id == paper_id)
    edges = edges_query.order_by(KnowledgeEdge.id.asc()).all()
    if paper_id is not None:
        paper_node_by_id = {
            node.paper_id: node.id for node in paper_nodes
        }
        target_paper_node_id = paper_node_by_id.get(paper_id)
        edges = [
            edge
            for edge in edges
            if edge.source_node_id in relevant_node_ids
            or edge.target_node_id in relevant_node_ids
            or (
                target_paper_node_id is not None
                and target_paper_node_id
                in {edge.source_node_id, edge.target_node_id}
            )
        ]
    relevant_node_ids.update(
        edge.source_node_id for edge in edges
    )
    relevant_node_ids.update(
        edge.target_node_id for edge in edges
    )
    node_rows = (
        db.query(KnowledgeNode)
        .filter(KnowledgeNode.id.in_(relevant_node_ids))
        .all()
        if relevant_node_ids
        else []
    )
    nodes = {
        node.id: node
        for node in node_rows
        if node.node_type in selected_node_types
    }
    if query.strip():
        normalized_query = query.strip().casefold()
        matching_ids = {
            node.id
            for node in nodes.values()
            if normalized_query in node.label.casefold()
        }
        neighbor_ids = set(matching_ids)
        for edge in edges:
            if edge.source_node_id in matching_ids:
                neighbor_ids.add(edge.target_node_id)
            if edge.target_node_id in matching_ids:
                neighbor_ids.add(edge.source_node_id)
        nodes = {
            node_id: node
            for node_id, node in nodes.items()
            if node_id in neighbor_ids
        }
    edges = [
        edge
        for edge in edges
        if edge.source_node_id in nodes and edge.target_node_id in nodes
    ]
    paper_map = {
        paper.id: paper
        for paper in db.query(Paper)
        .filter(Paper.id.in_({edge.paper_id for edge in edges}))
        .all()
    } if edges else {}
    degree = Counter()
    for edge in edges:
        degree[edge.source_node_id] += 1
        degree[edge.target_node_id] += 1
    global_ranks = _weighted_pagerank(
        nodes,
        [
            (
                edge.source_node_id,
                edge.target_node_id,
                RELATION_WEIGHTS.get(edge.relation_type, 0.5),
            )
            for edge in edges
        ],
    )
    paper_metrics = _paper_relation_metrics(nodes, edges)
    ranked_nodes = sorted(
        nodes.values(),
        key=lambda node: (
            paper_metrics.get(node.id, {}).get("is_core", False),
            global_ranks.get(node.id, 0.0),
            degree[node.id],
        ),
        reverse=True,
    )[: max(1, min(limit, 1000))]
    allowed = {node.id for node in ranked_nodes}
    edges = [
        edge
        for edge in edges
        if edge.source_node_id in allowed and edge.target_node_id in allowed
    ]
    node_payload = []
    for node in ranked_nodes:
        payload = {
            **_node_dict(node),
            "degree": degree[node.id],
            "pagerank": round(global_ranks.get(node.id, 0.0), 10),
            **paper_metrics.get(node.id, {}),
        }
        node_payload.append(payload)
    edge_payload = [
        _edge_dict(
            edge,
            source=nodes[edge.source_node_id],
            target=nodes[edge.target_node_id],
            paper=paper_map.get(edge.paper_id),
        )
        for edge in edges
    ]
    node_counts = Counter(node.node_type for node in ranked_nodes)
    relation_counts = Counter(edge.relation_type for edge in edges)
    return {
        "nodes": node_payload,
        "edges": edge_payload,
        "stats": {
            "node_count": len(node_payload),
            "edge_count": len(edge_payload),
            "paper_count": db.query(Paper).count(),
            "node_type_counts": dict(node_counts),
            "relation_type_counts": dict(relation_counts),
            "evidence_backed_edge_count": len(edge_payload),
            "missing_provenance_edge_count": 0,
            "core_paper_count": sum(
                1 for node in node_payload if node.get("is_core")
            ),
        },
        "latest_rebuild": _latest_run_dict(db),
        "disclaimer": KNOWLEDGE_GRAPH_DISCLAIMER,
    }


def get_knowledge_node_detail(db: Session, node_id: int) -> dict[str, Any]:
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise AppError(
            "知识图谱节点不存在。",
            code="KNOWLEDGE_GRAPH_NODE_NOT_FOUND",
            status_code=404,
            details={"node_id": node_id},
        )
    incident = (
        db.query(KnowledgeEdge)
        .filter(
            (KnowledgeEdge.source_node_id == node_id)
            | (KnowledgeEdge.target_node_id == node_id)
        )
        .order_by(KnowledgeEdge.id.asc())
        .all()
    )
    neighbor_ids = {
        edge.target_node_id
        if edge.source_node_id == node_id
        else edge.source_node_id
        for edge in incident
    }
    neighbor_rows = (
        db.query(KnowledgeNode)
        .filter(KnowledgeNode.id.in_(neighbor_ids))
        .all()
        if neighbor_ids
        else []
    )
    neighbors = {item.id: item for item in neighbor_rows}
    paper_ids = {edge.paper_id for edge in incident}
    papers = {
        paper.id: paper
        for paper in db.query(Paper).filter(Paper.id.in_(paper_ids)).all()
    } if paper_ids else {}
    relations = []
    for edge in incident:
        source = node if edge.source_node_id == node_id else neighbors.get(edge.source_node_id)
        target = node if edge.target_node_id == node_id else neighbors.get(edge.target_node_id)
        if source is None or target is None:
            continue
        payload = _edge_dict(
            edge,
            source=source,
            target=target,
            paper=papers.get(edge.paper_id),
        )
        payload["direction"] = (
            "outgoing" if edge.source_node_id == node_id else "incoming"
        )
        relations.append(payload)
    incoming = sum(
        1 for edge in incident if edge.target_node_id == node_id
    )
    outgoing = len(incident) - incoming
    all_nodes = {
        item.id: item for item in db.query(KnowledgeNode).all()
    }
    all_edges = db.query(KnowledgeEdge).all()
    global_ranks = _weighted_pagerank(
        all_nodes,
        [
            (
                edge.source_node_id,
                edge.target_node_id,
                RELATION_WEIGHTS.get(edge.relation_type, 0.5),
            )
            for edge in all_edges
        ],
    )
    paper_metrics = _paper_relation_metrics(all_nodes, all_edges)
    return {
        "node": {
            **_node_dict(node),
            "in_degree": incoming,
            "out_degree": outgoing,
            "degree": len(incident),
            "pagerank": round(global_ranks.get(node_id, 0.0), 10),
            **paper_metrics.get(node_id, {}),
        },
        "relations": relations,
        "disclaimer": KNOWLEDGE_GRAPH_DISCLAIMER,
    }


def get_core_papers(db: Session, *, limit: int = 10) -> dict[str, Any]:
    graph = get_knowledge_graph(db, limit=1000)
    nodes = {
        node["id"]: node
        for node in graph["nodes"]
        if node["node_type"] == "paper"
    }
    ordered = sorted(
        [
            node for node in nodes.values()
            if node.get("is_core")
        ],
        key=lambda node: (
            node.get("paper_pagerank", 0.0),
            node.get("paper_in_degree", 0),
            node.get("paper_evidence_count", 0),
        ),
        reverse=True,
    )[: max(1, min(limit, 50))]
    return {
        "papers": [
            {
                **node,
                "reasons": [
                    f"论文关系 PageRank 排名第 {node.get('paper_rank')}",
                    f"跨论文入度 {node.get('paper_in_degree', 0)}",
                    (
                        "共享科研实体关联 "
                        f"{node.get('paper_projected_connection_count', 0)} 篇论文"
                    ),
                    f"可定位证据 {node.get('paper_evidence_count', 0)} 条",
                ],
            }
            for node in ordered
        ],
        "result_count": len(ordered),
        "method": "weighted_pagerank_direct_and_entity_projection",
        "disclaimer": KNOWLEDGE_GRAPH_DISCLAIMER,
    }


def compare_methods(
    db: Session,
    *,
    paper_ids: Iterable[int],
) -> dict[str, Any]:
    requested = sorted(set(int(value) for value in paper_ids))
    if len(requested) < 2 or len(requested) > 5:
        raise AppError(
            "方法比较需要选择 2 至 5 篇论文。",
            code="KNOWLEDGE_GRAPH_INVALID_PAPER_SCOPE",
            status_code=422,
        )
    papers = {
        paper.id: paper
        for paper in db.query(Paper).filter(Paper.id.in_(requested)).all()
    }
    missing = [paper_id for paper_id in requested if paper_id not in papers]
    if missing:
        raise AppError(
            "部分论文不存在。",
            code="PAPER_NOT_FOUND",
            status_code=404,
            details={"paper_ids": missing},
        )
    method_edges = (
        db.query(KnowledgeEdge, KnowledgeNode)
        .join(
            KnowledgeNode,
            KnowledgeNode.id == KnowledgeEdge.target_node_id,
        )
        .filter(
            KnowledgeEdge.paper_id.in_(requested),
            KnowledgeEdge.relation_type == "USES_METHOD",
            KnowledgeNode.node_type == "method",
        )
        .order_by(KnowledgeEdge.id.asc())
        .all()
    )
    grouped: dict[int, dict[str, Any]] = {}
    for edge, method in method_edges:
        group = grouped.setdefault(
            method.id,
            {
                "method_node_id": method.id,
                "method": method.label,
                "paper_ids": [],
                "papers": [],
                "evidence": [],
            },
        )
        if edge.paper_id not in group["paper_ids"]:
            group["paper_ids"].append(edge.paper_id)
            paper = papers[edge.paper_id]
            group["papers"].append(
                {
                    "paper_id": paper.id,
                    "title": paper.title,
                    "publication_year": paper.publication_year,
                }
            )
        group["evidence"].append(
            {
                "edge_id": edge.id,
                "paper_id": edge.paper_id,
                "page_number": edge.page_number,
                "chunk_id": edge.chunk_id,
                "evidence_text": edge.evidence_text,
                "confidence": edge.confidence,
            }
        )
    methods = sorted(
        grouped.values(),
        key=lambda item: (len(item["paper_ids"]), len(item["evidence"])),
        reverse=True,
    )
    return {
        "paper_ids": requested,
        "methods": methods,
        "shared_methods": [
            method for method in methods if len(method["paper_ids"]) >= 2
        ],
        "unique_methods": [
            method for method in methods if len(method["paper_ids"]) == 1
        ],
        "coverage": {
            "papers_with_methods": len(
                {
                    edge.paper_id
                    for edge, _node in method_edges
                }
            ),
            "requested_paper_count": len(requested),
        },
        "disclaimer": KNOWLEDGE_GRAPH_DISCLAIMER,
    }


def _paper_node_id(db: Session, paper_id: int) -> int:
    node = (
        db.query(KnowledgeNode)
        .filter(
            KnowledgeNode.node_type == "paper",
            KnowledgeNode.paper_id == paper_id,
        )
        .first()
    )
    if node is None:
        raise AppError(
            "论文尚未进入知识图谱，请先重建图谱。",
            code="KNOWLEDGE_GRAPH_PAPER_NOT_INDEXED",
            status_code=409,
            details={"paper_id": paper_id},
        )
    return node.id


def find_relation_path(
    db: Session,
    *,
    source_paper_id: int,
    target_paper_id: int,
    max_depth: int = 5,
) -> dict[str, Any]:
    source_id = _paper_node_id(db, source_paper_id)
    target_id = _paper_node_id(db, target_paper_id)
    edges = db.query(KnowledgeEdge).order_by(KnowledgeEdge.id.asc()).all()
    adjacency: dict[int, list[tuple[int, KnowledgeEdge]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge.source_node_id].append((edge.target_node_id, edge))
        adjacency[edge.target_node_id].append((edge.source_node_id, edge))
    queue: deque[tuple[int, list[int], list[KnowledgeEdge]]] = deque(
        [(source_id, [source_id], [])]
    )
    visited = {source_id}
    found_nodes: list[int] = []
    found_edges: list[KnowledgeEdge] = []
    while queue:
        current, path_nodes, path_edges = queue.popleft()
        if current == target_id:
            found_nodes = path_nodes
            found_edges = path_edges
            break
        if len(path_edges) >= max_depth:
            continue
        for neighbor, edge in adjacency.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, [*path_nodes, neighbor], [*path_edges, edge]))
    node_rows = {
        node.id: node
        for node in db.query(KnowledgeNode)
        .filter(KnowledgeNode.id.in_(set(found_nodes)))
        .all()
    } if found_nodes else {}
    papers = {
        paper.id: paper
        for paper in db.query(Paper)
        .filter(
            Paper.id.in_(
                {edge.paper_id for edge in found_edges}
            )
        )
        .all()
    } if found_edges else {}
    return {
        "source_paper_id": source_paper_id,
        "target_paper_id": target_paper_id,
        "path_found": bool(found_nodes),
        "depth": max(0, len(found_edges)),
        "traversal": "undirected",
        "nodes": [
            _node_dict(node_rows[node_id])
            for node_id in found_nodes
            if node_id in node_rows
        ],
        "edges": [
            _edge_dict(
                edge,
                source=node_rows[edge.source_node_id],
                target=node_rows[edge.target_node_id],
                paper=papers.get(edge.paper_id),
            )
            for edge in found_edges
            if edge.source_node_id in node_rows
            and edge.target_node_id in node_rows
        ],
        "disclaimer": KNOWLEDGE_GRAPH_DISCLAIMER,
    }


def explore_research_gaps(
    db: Session,
    *,
    topic: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    nodes = {
        node.id: node for node in db.query(KnowledgeNode).all()
    }
    edges = db.query(KnowledgeEdge).order_by(KnowledgeEdge.id.asc()).all()
    papers = {
        paper.id: paper for paper in db.query(Paper).all()
    }
    normalized_topic = topic.strip().casefold()
    allowed_topic_ids = {
        node.id
        for node in nodes.values()
        if node.node_type == "topic"
        and (not normalized_topic or normalized_topic in node.label.casefold())
    }
    topic_edges = [
        edge
        for edge in edges
        if edge.relation_type == "STUDIES"
        and edge.target_node_id in nodes
        and nodes[edge.target_node_id].node_type == "topic"
    ]
    if normalized_topic:
        topic_edges = [
            edge for edge in topic_edges
            if edge.target_node_id in allowed_topic_ids
        ]
    topic_papers: dict[int, set[int]] = defaultdict(set)
    for edge in topic_edges:
        topic_papers[edge.target_node_id].add(edge.paper_id)
    gaps: list[dict[str, Any]] = []
    for topic_node_id, paper_ids in topic_papers.items():
        if len(paper_ids) > 2:
            continue
        node = nodes[topic_node_id]
        evidence = [
            edge for edge in topic_edges
            if edge.target_node_id == topic_node_id
        ]
        gaps.append(
            {
                "gap_type": (
                    "single_paper_topic"
                    if len(paper_ids) == 1
                    else "limited_topic_coverage"
                ),
                "title": f"主题“{node.label}”的本地覆盖较窄",
                "description": (
                    "当前只有少量本地论文提供了可定位的主题证据，"
                    "不能据此推断该方向已被充分研究。"
                ),
                "topic": node.label,
                "paper_ids": sorted(paper_ids),
                "paper_titles": [
                    papers[paper_id].title
                    for paper_id in sorted(paper_ids)
                    if paper_id in papers
                ],
                "evidence": [
                    {
                        "edge_id": edge.id,
                        "paper_id": edge.paper_id,
                        "page_number": edge.page_number,
                        "chunk_id": edge.chunk_id,
                        "evidence_text": edge.evidence_text,
                    }
                    for edge in evidence[:5]
                ],
            }
        )

    cross_semantic_pairs = {
        frozenset((edge.source_node_id, edge.target_node_id))
        for edge in edges
        if edge.relation_type in SEMANTIC_PAPER_RELATIONS
        and nodes[edge.source_node_id].node_type == "paper"
        and nodes[edge.target_node_id].node_type == "paper"
    }
    method_edges = [
        edge for edge in edges
        if edge.relation_type == "USES_METHOD"
        and nodes[edge.target_node_id].node_type == "method"
    ]
    method_papers: dict[int, set[int]] = defaultdict(set)
    method_evidence: dict[int, list[KnowledgeEdge]] = defaultdict(list)
    for edge in method_edges:
        method_papers[edge.target_node_id].add(edge.source_node_id)
        method_evidence[edge.target_node_id].append(edge)
    for method_node_id, paper_node_ids in method_papers.items():
        if len(paper_node_ids) < 2:
            continue
        compared = any(
            frozenset(pair) in cross_semantic_pairs
            for left_index, left in enumerate(sorted(paper_node_ids))
            for right in sorted(paper_node_ids)[left_index + 1:]
            for pair in [(left, right)]
        )
        if compared:
            continue
        method = nodes[method_node_id]
        gaps.append(
            {
                "gap_type": "method_without_cross_paper_comparison",
                "title": f"方法“{method.label}”缺少跨论文证据关系",
                "description": (
                    "多篇本地论文使用该方法，但当前没有带证据的 "
                    "SUPPORTS、CONTRADICTS 或 EXTENDS 跨论文关系。"
                ),
                "method": method.label,
                "paper_ids": sorted({
                    papers[edge.paper_id].id
                    for edge in method_evidence[method_node_id]
                    if edge.paper_id in papers
                }),
                "evidence": [
                    {
                        "edge_id": edge.id,
                        "paper_id": edge.paper_id,
                        "page_number": edge.page_number,
                        "chunk_id": edge.chunk_id,
                        "evidence_text": edge.evidence_text,
                    }
                    for edge in method_evidence[method_node_id][:6]
                ],
            }
        )

    for edge in edges:
        if edge.relation_type != "CONTRADICTS":
            continue
        source = nodes.get(edge.source_node_id)
        target = nodes.get(edge.target_node_id)
        if source is None or target is None:
            continue
        gaps.append(
            {
                "gap_type": "unresolved_contradiction",
                "title": f"“{source.label}”与“{target.label}”存在冲突线索",
                "description": (
                    "当前证据明确出现冲突语境，但系统不自动判断哪一结论正确。"
                ),
                "paper_ids": sorted({edge.paper_id}),
                "evidence": [
                    {
                        "edge_id": edge.id,
                        "paper_id": edge.paper_id,
                        "page_number": edge.page_number,
                        "chunk_id": edge.chunk_id,
                        "evidence_text": edge.evidence_text,
                    }
                ],
            }
        )
    gaps.sort(
        key=lambda item: (
            item["gap_type"] == "unresolved_contradiction",
            item["gap_type"] == "method_without_cross_paper_comparison",
        ),
        reverse=True,
    )
    selected = gaps[: max(1, min(limit, 100))]
    return {
        "topic_filter": topic.strip() or None,
        "gaps": selected,
        "result_count": len(selected),
        "method": "deterministic_graph_coverage_rules",
        "disclaimer": (
            "研究空白是基于当前本地图谱覆盖与关系证据的探索提示，"
            "不是对学术领域事实的自动裁决。"
        ),
    }


def rebuild_and_summarize(
    db: Session,
    paper_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    result = rebuild_knowledge_graph(db, paper_ids)
    graph = get_knowledge_graph(db, limit=50)
    return {
        **result,
        "stats": graph["stats"],
    }
