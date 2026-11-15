export interface PaperListItem {
  id: number;
  title: string;
  original_filename: string;
  stored_filename: string;
  file_size: number;
  content_type: string;
  page_count: number;
  char_count: number;
  authors: string | null;
  doi: string | null;
  publication_year: number | null;
  venue: string | null;
  pdf_metadata: Record<string, string | null> | null;
  parse_status: string;
  index_status: string;
  created_at: string;
  updated_at: string;
}

export interface PaperElement {
  id: number;
  element_index: number;
  element_type: string;
  text: string;
  data: Record<string, unknown> | unknown[] | null;
  bbox: number[] | null;
  section_path: string | null;
  extraction_status: string;
  confidence: number | null;
}

export interface PaperPage {
  page_number: number;
  text: string;
  char_count: number;
  parse_status: string;
  ocr_executed: boolean;
  ocr_error: string | null;
  checkpoint_status: string;
  checkpoint_error: string | null;
  checkpoint_generation: number;
  elements: PaperElement[];
}

export interface PaperDetail extends PaperListItem {
  pages: PaperPage[];
}

export interface ImportJobItem {
  id: string;
  original_filename: string;
  paper_id: number | null;
  status: string;
  parse_status: string;
  index_status: string;
  current_stage: string;
  progress: number;
  attempts: number;
  error_message: string | null;
  file_size: number;
  can_retry: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface ImportJob {
  id: string;
  kind: "import" | "index_rebuild";
  status: string;
  total_count: number;
  completed_count: number;
  failed_count: number;
  queued_count: number;
  running_count: number;
  progress: number;
  created_at: string | null;
  updated_at: string | null;
  items: ImportJobItem[];
}

export interface IndexConsistency {
  scope: "collection" | "papers";
  sqlite_chunk_count: number;
  chroma_vector_count: number;
  missing_vector_count: number;
  stale_vector_count: number;
  legacy_vector_count: number;
  orphan_vector_count: number;
  unverifiable_vector_count: number;
  read_error_count: number;
  parse_incomplete_page_count: number;
  parse_affected_paper_ids: number[];
  parse_issue_details: Array<{
    paper_id: number;
    expected_page_count: number;
    recorded_page_count: number;
    completed_page_count: number;
    failed_page_count: number;
    processing_page_count: number;
    pending_page_count: number;
    missing_page_count: number;
    incomplete_page_count: number;
  }>;
  missing_vector_ids: string[];
  stale_vector_ids: string[];
  legacy_vector_ids: string[];
  orphan_vector_ids: string[];
  affected_paper_ids: number[];
  orphan_paper_ids: number[];
  issues: Array<{
    status: "missing" | "stale" | "orphaned" | "unverifiable";
    vector_id: string;
    paper_id: number | null;
    chunk_id: number | null;
    reasons: string[];
  }>;
  index_schema_version: number;
  content_hash_algorithm: string;
  content_normalization_version: string;
  consistent: boolean;
}

export interface SimilarPaperCandidate {
  paper_id: number;
  title: string;
  authors: string | null;
  created_at: string | null;
  semantic_similarity: number;
  title_similarity: number;
  metadata_similarity?: number;
  match_reason: string;
  candidate_type: "suspected_version" | "possible_duplicate";
}

export interface PaperVersion {
  paper_id: number;
  version_number: number;
  version_label: string;
  source: string;
  retained: boolean;
  created_at: string | null;
  title: string;
  original_filename: string;
  created_at_paper: string | null;
}

export interface HealthData {
  name: string;
  status: string;
  env: string;
  version: string;
  features: Record<string, string>;
}
