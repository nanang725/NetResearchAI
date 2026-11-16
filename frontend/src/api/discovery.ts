import { http } from "./http";
import type {
  DiscoveryField,
  DiscoveryRecommendations,
  DiscoverySortBy,
  DiscoverySortOrder,
  ExternalFavorite,
  ExternalPaperResult,
  ExternalSearchResult,
  TrackingSearch,
} from "@/types/discovery";


export async function searchExternalLiterature(params: {
  query: string;
  field: DiscoveryField;
  page: number;
  pageSize: number;
  sortBy: DiscoverySortBy;
  sortOrder: DiscoverySortOrder;
}): Promise<ExternalSearchResult> {
  const { data } = await http.get<ExternalSearchResult>("/discovery/search", {
    params: {
      query: params.query,
      field: params.field,
      page: params.page,
      page_size: params.pageSize,
      sort_by: params.sortBy,
      sort_order: params.sortOrder,
    },
  });
  return data;
}


export async function favoriteExternal(
  arxivId: string,
): Promise<ExternalFavorite> {
  const { data } = await http.post<ExternalFavorite>("/discovery/favorites", {
    arxiv_id: arxivId,
  });
  return data;
}


export async function fetchExternalFavorites(): Promise<ExternalFavorite[]> {
  const { data } = await http.get<ExternalFavorite[]>("/discovery/favorites");
  return data ?? [];
}


export async function importFavoritePdf(favoriteId: number) {
  const { data } = await http.post<{
    status: string;
    favorite: ExternalFavorite;
    import_job: ExternalFavorite["import_job"];
  }>(`/discovery/favorites/${favoriteId}/import`);
  return data;
}


export async function createDiscoveryTracking(payload: {
  name: string;
  query: string;
  field: DiscoveryField;
  sort_by: DiscoverySortBy;
  sort_order: DiscoverySortOrder;
  page_size: number;
}): Promise<TrackingSearch> {
  const { data } = await http.post<TrackingSearch>("/discovery/tracking", payload);
  return data;
}


export async function fetchDiscoveryTrackings(): Promise<TrackingSearch[]> {
  const { data } = await http.get<TrackingSearch[]>("/discovery/tracking");
  return data ?? [];
}


export async function refreshDiscoveryTracking(
  trackingId: number,
): Promise<TrackingSearch> {
  const { data } = await http.post<TrackingSearch>(
    `/discovery/tracking/${trackingId}/refresh`,
  );
  return data;
}


export async function fetchDiscoveryRecommendations(): Promise<DiscoveryRecommendations> {
  const { data } = await http.get<DiscoveryRecommendations>(
    "/discovery/recommendations",
  );
  return data;
}


export function patchFavoriteState(
  result: ExternalPaperResult,
  favorite: ExternalFavorite,
): ExternalPaperResult {
  return {
    ...result,
    favorite_id: favorite.favorite_id,
  };
}
