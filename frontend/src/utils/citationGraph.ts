import type {
  CitationGraphData,
  LiteratureReviewRequest,
} from "@/types/citation";


export interface CitationGraphPalette {
  foundational: string;
  connected: string;
  isolated: string;
}


export function citationEChartsSeries(
  data: CitationGraphData,
  palette: CitationGraphPalette = {
    foundational: "#c9842a",
    connected: "#2d6f95",
    isolated: "#9aa8b3",
  },
) {
  return {
    data: data.nodes.map((node) => ({
      id: node.id,
      name: node.title,
      paper_id: node.paper_id,
      value: node.pagerank,
      category: node.is_foundational
        ? 0
        : node.in_degree === 0 && node.out_degree === 0
          ? 2
          : 1,
      symbolSize: Math.min(
        56,
        20 + node.in_degree * 5 + Math.sqrt(node.pagerank) * 30,
      ),
      itemStyle: {
        color: node.is_foundational
          ? palette.foundational
          : node.in_degree === 0 && node.out_degree === 0
            ? palette.isolated
            : palette.connected,
      },
      raw: node,
    })),
    links: data.edges.map((edge) => ({
      source: edge.source,
      target: edge.target,
      reference_id: edge.reference_id,
      value: edge.match_confidence,
    })),
    categories: [
      { name: "核心论文" },
      { name: "引用网络论文" },
      { name: "孤立论文" },
    ],
  };
}


export function paperPageRoute(paperId: number, pageNumber = 1) {
  return {
    name: "paper-detail",
    params: { id: paperId },
    query: { page: String(Math.max(1, pageNumber)) },
  };
}


export function validateReviewPaperSelection(paperIds: number[]): string | null {
  if (paperIds.length < 2) return "请选择至少 2 篇论文。";
  if (paperIds.length > 5) return "最多选择 5 篇论文。";
  if (new Set(paperIds).size !== paperIds.length) return "不能重复选择论文。";
  return null;
}


export function buildReviewRequest(
  paperIds: number[],
  topic: string,
  includeFutureWork: boolean,
): LiteratureReviewRequest {
  return {
    paper_ids: paperIds,
    topic: topic.trim(),
    include_future_work: includeFutureWork,
  };
}
