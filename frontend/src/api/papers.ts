import { http } from "./http";
import type {
  HealthData,
  IndexConsistency,
  ImportJob,
  PaperDetail,
  PaperListItem,
  PaperVersion,
  SimilarPaperCandidate,
  TranslationRequest,
  TranslationResult,
} from "@/types/paper";

export async function fetchHealth(): Promise<HealthData> {
  const { data } = await http.get<HealthData>("/health");
  return data;
}

export async function fetchPapers(): Promise<PaperListItem[]> {
  const { data } = await http.get<PaperListItem[]>("/papers");
  return data ?? [];
}

export async function fetchPaper(id: number): Promise<PaperDetail> {
  const { data } = await http.get<PaperDetail>(`/papers/${id}`);
  return data;
}

export async function translatePaperText(
  id: number,
  request: TranslationRequest,
): Promise<TranslationResult> {
  const { data } = await http.post<TranslationResult>(
    `/papers/${id}/translate`,
    request,
  );
  return data;
}

export async function uploadPaper(file: File, onProgress?: (percent: number) => void): Promise<PaperDetail> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await http.post<PaperDetail>("/papers/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (event) => {
      if (!onProgress || !event.total) return;
      onProgress(Math.round((event.loaded / event.total) * 100));
    },
  });
  return data;
}

export async function createImportJob(files: File[]): Promise<ImportJob> {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  const { data } = await http.post<ImportJob>("/papers/import-jobs", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function fetchImportJobs(): Promise<ImportJob[]> {
  const { data } = await http.get<ImportJob[]>("/papers/import-jobs");
  return data ?? [];
}

export async function fetchImportJob(id: string): Promise<ImportJob> {
  const { data } = await http.get<ImportJob>(`/papers/import-jobs/${id}`);
  return data;
}

export async function retryImportJob(id: string): Promise<ImportJob> {
  const { data } = await http.post<ImportJob>(`/papers/import-jobs/${id}/retry`);
  return data;
}

export async function retryImportItem(jobId: string, itemId: string): Promise<ImportJob> {
  const { data } = await http.post<ImportJob>(
    `/papers/import-jobs/${jobId}/items/${itemId}/retry`,
  );
  return data;
}

export async function fetchIndexConsistency(): Promise<IndexConsistency> {
  const { data } = await http.get<IndexConsistency>("/papers/index/consistency");
  return data;
}

export async function requestIndexRebuild(): Promise<ImportJob> {
  const { data } = await http.post<ImportJob>("/papers/index/rebuild");
  return data;
}

export async function fetchSimilarPapers(id: number): Promise<SimilarPaperCandidate[]> {
  const { data } = await http.get<SimilarPaperCandidate[]>(`/papers/${id}/similar`);
  return data ?? [];
}

export async function fetchPaperVersions(id: number): Promise<PaperVersion[]> {
  const { data } = await http.get<PaperVersion[]>(`/papers/${id}/versions`);
  return data ?? [];
}

export async function addPaperVersion(id: number, basedOnId: number): Promise<PaperVersion[]> {
  const { data } = await http.post<PaperVersion[]>(`/papers/${id}/versions`, {
    based_on_id: basedOnId,
    source: "manual",
  });
  return data ?? [];
}

export async function retainPaperVersion(id: number, retainedPaperId: number): Promise<PaperVersion[]> {
  const { data } = await http.post<PaperVersion[]>(
    `/papers/${id}/versions/${retainedPaperId}/retain`,
  );
  return data ?? [];
}

export async function comparePaperVersions(id: number, otherId: number): Promise<{ diff: string; truncated: boolean }> {
  const { data } = await http.get<{ diff: string; truncated: boolean }>(
    `/papers/${id}/versions/${otherId}/diff`,
  );
  return data;
}

export async function deletePaperVersion(id: number, versionId: number): Promise<void> {
  await http.delete(`/papers/${id}/versions/${versionId}`);
}

export async function deletePaper(id: number): Promise<void> {
  await http.delete(`/papers/${id}`);
}

export function searchPapers(q: string) {
  return http.get('/papers/search', {
    params: { q }
  })
}

export interface ChatHistoryMessage {
  role: 'user' | 'assistant'
  content: string
}

export async function chatWithAgent(
  query: string,
  paperId?: number,
  history?: ChatHistoryMessage[],
) {
  const { data } = await http.post('/papers/agent/chat', history ?? null, {
    params: { 
      query,
      paper_id: paperId // 如果是 undefined 就不会传这个参数
    }
  });
  return data;
}
