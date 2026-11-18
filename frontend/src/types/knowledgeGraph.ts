export type KnowledgeNodeType =
  | "paper"
  | "author"
  | "topic"
  | "method"
  | "dataset"
  | "model"
  | "finding"
  | "institution";

export type KnowledgeRelationType =
  | "CITES"
  | "AUTHORED_BY"
  | "STUDIES"
  | "USES_METHOD"
  | "USES_DATASET"
  | "USES_MODEL"
  | "SUPPORTS"
  | "CONTRADICTS"
  | "EXTENDS"
  | "AFFILIATED_WITH";

export interface KnowledgeGraphNode {
  id: number;
  node_type: KnowledgeNodeType;
  label: string;
  normalized_name: string;
  paper_id: number | null;
  attributes: Record<string, unknown>;
  degree: number;
  pagerank: number;
  paper_pagerank?: number;
  paper_rank?: number;
  paper_in_degree?: number;
  paper_out_degree?: number;
  paper_evidence_count?: number;
  paper_projected_connection_count?: number;
  is_core?: boolean;
}

export interface KnowledgeGraphEdge {
  id: number;
  source: number;
  target: number;
  source_label: string;
  target_label: string;
  source_type: KnowledgeNodeType;
  target_type: KnowledgeNodeType;
  relation_type: KnowledgeRelationType;
  paper_id: number;
  paper_title: string | null;
  page_number: number;
  chunk_id: number;
  evidence_text: string;
  confidence: number;
  extraction_method: string;
  attributes: Record<string, unknown>;
  direction?: "incoming" | "outgoing";
}

export interface KnowledgeExtractionRun {
  run_id: string;
  status: "running" | "completed" | "failed";
  paper_count: number;
  node_count: number;
  edge_count: number;
  skipped_chunk_count: number;
  diagnostics: string[];
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface KnowledgeGraphData {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  stats: {
    node_count: number;
    edge_count: number;
    paper_count: number;
    node_type_counts: Partial<Record<KnowledgeNodeType, number>>;
    relation_type_counts: Partial<Record<KnowledgeRelationType, number>>;
    evidence_backed_edge_count: number;
    missing_provenance_edge_count: number;
    core_paper_count: number;
  };
  latest_rebuild: KnowledgeExtractionRun | null;
  disclaimer: string;
}

export interface KnowledgeNodeDetail {
  node: KnowledgeGraphNode & {
    in_degree: number;
    out_degree: number;
  };
  relations: KnowledgeGraphEdge[];
  disclaimer: string;
}

export interface CoreKnowledgePaper extends KnowledgeGraphNode {
  reasons: string[];
}

export interface CoreKnowledgePapersData {
  papers: CoreKnowledgePaper[];
  result_count: number;
  method: string;
  disclaimer: string;
}

export interface KnowledgeMethod {
  method_node_id: number;
  method: string;
  paper_ids: number[];
  papers: Array<{
    paper_id: number;
    title: string;
    publication_year: number | null;
  }>;
  evidence: Array<{
    edge_id: number;
    paper_id: number;
    page_number: number;
    chunk_id: number;
    evidence_text: string;
    confidence: number;
  }>;
}

export interface KnowledgeMethodComparisonData {
  paper_ids: number[];
  methods: KnowledgeMethod[];
  shared_methods: KnowledgeMethod[];
  unique_methods: KnowledgeMethod[];
  coverage: {
    papers_with_methods: number;
    requested_paper_count: number;
  };
  disclaimer: string;
}

export interface KnowledgePathData {
  source_paper_id: number;
  target_paper_id: number;
  path_found: boolean;
  depth: number;
  traversal: "undirected";
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  disclaimer: string;
}

export interface KnowledgeGap {
  gap_type:
    | "single_paper_topic"
    | "limited_topic_coverage"
    | "method_without_cross_paper_comparison"
    | "unresolved_contradiction";
  title: string;
  description: string;
  paper_ids: number[];
  paper_titles?: string[];
  topic?: string;
  method?: string;
  evidence: Array<{
    edge_id: number;
    paper_id: number;
    page_number: number;
    chunk_id: number;
    evidence_text: string;
  }>;
}

export interface KnowledgeGapsData {
  topic_filter: string | null;
  gaps: KnowledgeGap[];
  result_count: number;
  method: string;
  disclaimer: string;
}

export interface KnowledgeGraphRebuildResult {
  run_id: string;
  status: string;
  paper_ids: number[];
  paper_count: number;
  node_count: number;
  edge_count: number;
  skipped_chunk_count: number;
  diagnostics: string[];
  extraction_version: string;
  stats: KnowledgeGraphData["stats"];
}
