import type { ImportJob } from "@/types/paper";


export type DiscoveryField = "all" | "title" | "abstract" | "author";
export type DiscoverySortBy = "relevance" | "last_updated" | "submitted";
export type DiscoverySortOrder = "ascending" | "descending";


export interface ExternalPaperResult {
  external_paper_id?: number;
  arxiv_id: string;
  title: string;
  authors: string[];
  summary: string;
  published: string | null;
  updated: string | null;
  primary_category: string | null;
  categories?: string[];
  abs_url: string;
  pdf_url: string;
  origin: "arxiv_external";
  in_local_library: boolean;
  local_paper_id: number | null;
  favorite_id: number | null;
  is_new?: boolean;
}


export interface ExternalSearchResult {
  query: string;
  field: DiscoveryField;
  page: number;
  page_size: number;
  sort_by: DiscoverySortBy;
  sort_order: DiscoverySortOrder;
  total_results: number;
  total_pages: number;
  results: ExternalPaperResult[];
  source: "arxiv_official_api";
}


export interface ExternalFavorite extends ExternalPaperResult {
  favorite_created_at: string;
  import_status: string;
  import_error: string | null;
  import_job: ImportJob | null;
}


export interface TrackingHistoryRun {
  run_id: number;
  tracking_id: number;
  status: "running" | "completed" | "failed";
  started_at: string | null;
  finished_at: string | null;
  total_results: number;
  new_result_count: number;
  error_message: string | null;
  results?: ExternalPaperResult[];
}


export interface TrackingSearch {
  tracking_id: number;
  name: string;
  query: string;
  field: DiscoveryField;
  sort_by: DiscoverySortBy;
  sort_order: DiscoverySortOrder;
  page_size: number;
  created_at: string;
  last_refreshed_at: string | null;
  run_count: number;
  new_count_latest: number;
  latest_run: TrackingHistoryRun | null;
  history: TrackingHistoryRun[];
}


export interface LocalRecommendation {
  origin: "local_library";
  paper_id: number;
  title: string;
  authors: string | null;
  publication_year: number | null;
  page_number: number;
  chunk_id: number;
  evidence: string;
}


export interface DiscoveryRecommendations {
  seed: {
    source: "tracking_keyword" | "favorite_paper";
    query: string;
  };
  external_results: ExternalPaperResult[];
  local_recommendations: LocalRecommendation[];
  local_rag_status: "available" | "unavailable";
  source_distinction: Record<string, string>;
  disclaimer: string;
}


export interface DailyRecommendationProfile {
  interests: string[];
  configured: boolean;
  updated_at: string | null;
}


export interface DailyRecommendationPaper extends ExternalPaperResult {
  item_id: number;
  score: number;
  matched_interests: string[];
  recommendation_reason: string;
  reason_source: "deterministic_keyword_rules";
  is_read: boolean;
  position: number;
  favorite_import_status: string;
  favorite_import_error: string | null;
}


export interface DailyRecommendationBatch {
  batch_id: number;
  date: string;
  status: "queued" | "running" | "completed" | "failed";
  interests: string[];
  new_item_count: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  items: DailyRecommendationPaper[];
  source: "arxiv_official_api";
  reason_disclaimer: string;
}


export interface DailyRecommendationState {
  date: string;
  profile: DailyRecommendationProfile;
  current: DailyRecommendationBatch | null;
  latest: DailyRecommendationBatch | null;
}
