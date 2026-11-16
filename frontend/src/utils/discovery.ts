import type {
  DiscoveryField,
  DiscoverySortBy,
  DiscoverySortOrder,
  ExternalPaperResult,
} from "@/types/discovery";


export function formatArxivDate(value: string | null): string {
  if (!value) return "日期未知";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "日期未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(parsed);
}


export function buildTrackingPayload(input: {
  name: string;
  query: string;
  field: DiscoveryField;
  sortBy: DiscoverySortBy;
  sortOrder: DiscoverySortOrder;
  pageSize: number;
}) {
  const name = input.name.trim();
  const query = input.query.trim();
  if (!name) throw new Error("追踪名称不能为空");
  if (query.length < 2) throw new Error("追踪关键词至少需要 2 个字符");
  return {
    name,
    query,
    field: input.field,
    sort_by: input.sortBy,
    sort_order: input.sortOrder,
    page_size: Math.max(1, Math.min(50, Math.round(input.pageSize))),
  };
}


export function externalResultLabel(result: ExternalPaperResult): string {
  if (result.in_local_library) return "本地已有";
  if (result.favorite_id) return "已收藏";
  return "arXiv 外部结果";
}


export function favoriteImportStatusLabel(status: string): string {
  return {
    not_imported: "未入库",
    already_in_library: "本地已有",
    queued: "等待导入",
    running: "导入中",
    succeeded: "已导入",
    imported: "已导入",
    failed: "入库失败",
  }[status] ?? status;
}


export function favoriteImportStatusType(
  status: string,
): "success" | "warning" | "danger" | "info" {
  if (["succeeded", "imported", "already_in_library"].includes(status)) {
    return "success";
  }
  if (status === "failed") return "danger";
  if (["queued", "running"].includes(status)) return "warning";
  return "info";
}
