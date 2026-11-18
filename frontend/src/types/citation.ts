export interface CitationNode {
  id: string;
  paper_id: number;
  title: string;
  year: number | null;
  authors: string[];
  in_degree: number;
  out_degree: number;
  pagerank: number;
  rank: number;
  is_foundational: boolean;
  foundational_reasons: string[];
}

export interface CitationGraphEdge {
  id: number;
  source: string;
  target: string;
  reference_id: number;
  raw_text: string;
  source_page: number;
  match_method: string;
  match_confidence: number;
}

export interface CitationGraphStats {
  paper_count: number;
  displayed_node_count: number;
  displayed_edge_count: number;
  independent_paper_count: number;
  reference_count: number;
  matched_reference_count: number;
  unmatched_reference_count: number;
  ambiguous_reference_count: number;
  edge_count: number;
  foundational_paper_count: number;
}

export interface CitationGraphData {
  nodes: CitationNode[];
  edges: CitationGraphEdge[];
  stats: CitationGraphStats;
  disclaimer: string;
}

export interface CitationRelation {
  paper_id: number;
  title: string;
  year: number | null;
  reference_id: number;
  raw_reference: string;
  source_page: number;
  match_method: string;
  match_confidence: number;
}

export interface CitationPaperDetail {
  paper_id: number;
  title: string;
  year: number | null;
  in_degree: number;
  out_degree: number;
  pagerank: number;
  rank: number;
  is_foundational: boolean;
  foundational_reasons: string[];
  outgoing: CitationRelation[];
  incoming: CitationRelation[];
}

export interface CitationRebuildResult {
  rebuilt_paper_ids: number[];
  reference_count: number;
  matched_reference_count: number;
  ambiguous_reference_count: number;
  unmatched_reference_count: number;
  edge_count: number;
}

export interface ReviewEvidenceCitation {
  paper_id: number;
  title: string;
  page_number: number;
  chunk_id: number;
  evidence: string;
}

export interface LiteratureReviewSection {
  heading: string;
  content: string;
  citations: ReviewEvidenceCitation[];
}

export interface ReviewPaperCoverage {
  paper_id: number;
  title: string;
  evidence_count: number;
  status: "covered" | "insufficient_evidence";
}

export interface ReviewComparisonFinding {
  paper_id: number;
  title: string;
  summary: string;
  claim_kind: "evidence" | "inference" | "insufficient_evidence";
  evidence_status: "covered" | "insufficient_evidence";
  citations: ReviewEvidenceCitation[];
}

export interface ReviewDimensionComparison {
  dimension: "methods" | "results" | "conclusions" | "limitations";
  label: string;
  items: ReviewComparisonFinding[];
}

export interface ReviewFutureWorkItem {
  paper_id: number;
  title: string;
  page_number: number;
  chunk_id: number;
  kind: "explicit" | "inferred";
  original_evidence: string;
  summary: string;
}

export interface LiteratureReview {
  title: string;
  topic: string;
  selected_paper_ids: number[];
  sections: LiteratureReviewSection[];
  paper_coverage: ReviewPaperCoverage[];
  dimension_comparison: ReviewDimensionComparison[];
  future_work: ReviewFutureWorkItem[];
  sources: ReviewEvidenceCitation[];
  model: string;
  disclaimer: string;
}

export interface LiteratureReviewRequest {
  paper_ids: number[];
  topic: string;
  include_future_work: boolean;
}

export interface LiteratureReviewSession {
  review_id: string;
  selected_paper_ids: number[];
  topic: string;
  include_future_work: boolean;
  status: "queued" | "running" | "completed" | "failed";
  result: LiteratureReview | null;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}
