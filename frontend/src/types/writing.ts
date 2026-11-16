export type TranslationDirection = "zh_to_en" | "en_to_zh";

export interface OutlineEvidence {
  citation_id: number;
  paper_id: number;
  title: string;
  page_number: number;
  chunk_id: number;
  section_label: string;
  excerpt: string;
}

export interface OutlineSection {
  level: number;
  heading: string;
  writing_points: string[];
  evidence: OutlineEvidence[];
  evidence_status: "covered" | "insufficient_evidence";
  unsupported_note: string | null;
}

export interface AcademicOutline {
  paper: {
    paper_id: number;
    title: string;
    authors: string | null;
    publication_year: number | null;
    doi: string | null;
  };
  topic: string;
  title: string;
  sections: OutlineSection[];
  evidence_sources: OutlineEvidence[];
  evidence_chunk_count: number;
  generated_by: string;
  content_labels: Record<string, string>;
  disclaimer: string;
}

export interface VerifiableReference {
  paper_id: number;
  verified_local: boolean;
  can_cite: boolean;
  title: string | null;
  authors: string | null;
  year: number | null;
  venue: string | null;
  doi: string | null;
  unknown_fields: string[];
  metadata_sources: Record<string, string>;
  citation_text: string;
  bibtex: string;
  evidence: OutlineEvidence | null;
  verification_note: string;
}

export interface VerifiableReferences {
  references: VerifiableReference[];
  total_count: number;
  verified_count: number;
  unknown_field_count: number;
  disclaimer: string;
  content_labels: Record<string, string>;
}

export interface AcademicTranslation {
  direction: TranslationDirection;
  source_language: string;
  target_language: string;
  original_text: string;
  translated_text: string;
  protected_token_count: number;
  model: string;
  disclaimer: string;
}

export type CsvFieldType =
  | "integer"
  | "number"
  | "boolean"
  | "date"
  | "text";

export interface CsvFieldProfile {
  name: string;
  type: CsvFieldType;
  count: number;
  missing: number;
  missing_rate: number;
  unique: number;
  statistics: {
    min: number | null;
    max: number | null;
    mean: number | null;
    median: number | null;
    stdev: number | null;
  };
  top_values: Array<{
    value: string;
    count: number;
    ratio: number;
  }>;
}

export interface CsvChart {
  chart_type: "bar";
  title: string;
  series_name: string;
  categories: string[];
  values: number[];
  aggregation: string;
  category_column: string;
  value_column: string | null;
  caption: string;
  analysis: string;
  data_origin: "uploaded_csv_aggregation";
  data_sha256: string;
}

export interface CsvAnalysis {
  filename: string;
  row_count: number;
  column_count: number;
  missing_cell_count: number;
  fields: Array<{
    name: string;
    type: CsvFieldType;
    missing: number;
    missing_rate: number;
    unique: number;
  }>;
  profiles: CsvFieldProfile[];
  chart: CsvChart;
  content_labels: Record<string, string>;
  disclaimer: string;
}
