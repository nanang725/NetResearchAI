export interface PaperListItem {
  id: number;
  title: string;
  original_filename: string;
  file_size: number;
  page_count: number;
  char_count: number;
  authors: string | null;
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
  content_type: string;
  stored_filename: string;
  pdf_metadata: Record<string, string> | null;
  pages: PaperPage[];
}

export interface HealthData {
  name: string;
  status: string;
  env: string;
  version: string;
  features: Record<string, string>;
}
