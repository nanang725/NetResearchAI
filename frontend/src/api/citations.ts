import { http } from "./http";
import type {
  CitationGraphData,
  CitationPaperDetail,
  CitationRebuildResult,
  LiteratureReview,
  LiteratureReviewRequest,
  LiteratureReviewSession,
} from "@/types/citation";


export interface CitationGraphParams {
  limit?: number;
  min_citations?: number;
  paper_id?: number;
  include_isolated?: boolean;
}


export async function fetchCitationGraph(
  params: CitationGraphParams = {},
): Promise<CitationGraphData> {
  const { data } = await http.get<CitationGraphData>("/citations/graph", {
    params,
  });
  return data;
}


export async function fetchCitationPaper(
  paperId: number,
): Promise<CitationPaperDetail> {
  const { data } = await http.get<CitationPaperDetail>(
    `/citations/papers/${paperId}`,
  );
  return data;
}


export async function rebuildCitations(
  paperIds?: number[],
): Promise<CitationRebuildResult> {
  const { data } = await http.post<CitationRebuildResult>(
    "/citations/rebuild",
    paperIds === undefined ? {} : { paper_ids: paperIds },
  );
  return data;
}


export async function rebuildPaperCitations(
  paperId: number,
): Promise<CitationRebuildResult> {
  const { data } = await http.post<CitationRebuildResult>(
    `/citations/papers/${paperId}/rebuild`,
  );
  return data;
}


export async function generateLiteratureReview(
  request: LiteratureReviewRequest,
): Promise<LiteratureReview> {
  const { data } = await http.post<LiteratureReview>("/reviews", request);
  return data;
}


export async function createLiteratureReviewSession(
  request: LiteratureReviewRequest,
): Promise<LiteratureReviewSession> {
  const { data } = await http.post<LiteratureReviewSession>(
    "/reviews/sessions",
    request,
  );
  return data;
}


export async function fetchLiteratureReviewSession(
  reviewId: string,
): Promise<LiteratureReviewSession> {
  const { data } = await http.get<LiteratureReviewSession>(
    `/reviews/sessions/${reviewId}`,
  );
  return data;
}


export async function fetchLatestLiteratureReviewSession(): Promise<
  LiteratureReviewSession | null
> {
  const { data } = await http.get<LiteratureReviewSession | null>(
    "/reviews/sessions/latest",
  );
  return data;
}


export async function retryLiteratureReviewSession(
  reviewId: string,
): Promise<LiteratureReviewSession> {
  const { data } = await http.post<LiteratureReviewSession>(
    `/reviews/sessions/${reviewId}/retry`,
  );
  return data;
}
