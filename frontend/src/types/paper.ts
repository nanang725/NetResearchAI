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
  pdf_metadata: Record<string, string | null> | null;
  parse_status: string;
  index_status: string;
  created_at: string;
  updated_at: string;
}

export interface PaperPage {
  page_number: number;
  text: string;
  char_count: number;
}

export interface PaperDetail extends PaperListItem {
  pages: PaperPage[];
}

export interface HealthData {
  name: string;
  status: string;
  env: string;
  version: string;
  features: Record<string, string>;
}