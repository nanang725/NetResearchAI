import { http } from "./http";
import type {
  CoreKnowledgePapersData,
  KnowledgeGapsData,
  KnowledgeGraphData,
  KnowledgeGraphRebuildResult,
  KnowledgeMethodComparisonData,
  KnowledgeNodeDetail,
  KnowledgeNodeType,
  KnowledgePathData,
  KnowledgeRelationType,
} from "@/types/knowledgeGraph";


export interface KnowledgeGraphParams {
  node_types?: KnowledgeNodeType[];
  relation_types?: KnowledgeRelationType[];
  paper_id?: number;
  query?: string;
  limit?: number;
}


export async function fetchKnowledgeGraph(
  params: KnowledgeGraphParams = {},
): Promise<KnowledgeGraphData> {
  const { data } = await http.get<KnowledgeGraphData>(
    "/knowledge-graph",
    {
      params,
      paramsSerializer: {
        indexes: null,
      },
    },
  );
  return data;
}


export async function fetchKnowledgeNode(
  nodeId: number,
): Promise<KnowledgeNodeDetail> {
  const { data } = await http.get<KnowledgeNodeDetail>(
    `/knowledge-graph/entities/${nodeId}`,
  );
  return data;
}


export async function fetchCoreKnowledgePapers(
  limit = 10,
): Promise<CoreKnowledgePapersData> {
  const { data } = await http.get<CoreKnowledgePapersData>(
    "/knowledge-graph/analysis/core-papers",
    { params: { limit } },
  );
  return data;
}


export async function fetchKnowledgeMethodComparison(
  paperIds: number[],
): Promise<KnowledgeMethodComparisonData> {
  const { data } = await http.get<KnowledgeMethodComparisonData>(
    "/knowledge-graph/analysis/method-comparison",
    {
      params: { paper_ids: paperIds },
      paramsSerializer: {
        indexes: null,
      },
    },
  );
  return data;
}


export async function fetchKnowledgePath(
  sourcePaperId: number,
  targetPaperId: number,
  maxDepth = 5,
): Promise<KnowledgePathData> {
  const { data } = await http.get<KnowledgePathData>(
    "/knowledge-graph/path",
    {
      params: {
        source_paper_id: sourcePaperId,
        target_paper_id: targetPaperId,
        max_depth: maxDepth,
      },
    },
  );
  return data;
}


export async function fetchResearchGaps(
  topic = "",
  limit = 20,
): Promise<KnowledgeGapsData> {
  const { data } = await http.get<KnowledgeGapsData>(
    "/knowledge-graph/research-gaps",
    { params: { topic, limit } },
  );
  return data;
}


export async function rebuildKnowledgeGraph(
  paperIds?: number[],
): Promise<KnowledgeGraphRebuildResult> {
  const { data } = await http.post<KnowledgeGraphRebuildResult>(
    "/knowledge-graph/rebuild",
    paperIds === undefined ? {} : { paper_ids: paperIds },
  );
  return data;
}
