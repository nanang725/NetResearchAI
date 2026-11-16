import { http } from "./http";
import type {
  AcademicOutline,
  AcademicTranslation,
  CsvAnalysis,
  TranslationDirection,
  VerifiableReferences,
} from "@/types/writing";

export async function generateAcademicOutline(
  paperId: number,
  topic: string,
): Promise<AcademicOutline> {
  const { data } = await http.post<AcademicOutline>("/writing/outline", {
    paper_id: paperId,
    topic,
  });
  return data;
}

export async function fetchVerifiableReferences(
  paperIds: number[],
): Promise<VerifiableReferences> {
  const { data } = await http.post<VerifiableReferences>("/writing/references", {
    paper_ids: paperIds,
  });
  return data;
}

export async function translateAcademicContent(
  text: string,
  direction: TranslationDirection,
): Promise<AcademicTranslation> {
  const { data } = await http.post<AcademicTranslation>("/writing/translate", {
    text,
    direction,
  });
  return data;
}

export async function analyzeCsv(file: File): Promise<CsvAnalysis> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await http.post<CsvAnalysis>("/datasets/analyze", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}
