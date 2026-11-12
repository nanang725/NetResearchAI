import { http } from "./http";
import type { HealthData, PaperDetail, PaperListItem } from "@/types/paper";

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

export async function deletePaper(id: number): Promise<void> {
  await http.delete(`/papers/${id}`);
}

// 检索论文片段
export function searchPapers(q: string) {
  return http.get('/papers/search', {
    params: { q }
  })
}