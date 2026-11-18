import type {
  KnowledgeGraphData,
  KnowledgeGraphNode,
  KnowledgeNodeType,
} from "@/types/knowledgeGraph";


export const KNOWLEDGE_NODE_TYPES: Array<{
  value: KnowledgeNodeType;
  label: string;
}> = [
  { value: "paper", label: "论文" },
  { value: "author", label: "作者" },
  { value: "topic", label: "主题" },
  { value: "method", label: "方法" },
  { value: "dataset", label: "数据集" },
  { value: "model", label: "模型" },
  { value: "finding", label: "发现" },
  { value: "institution", label: "机构" },
];


export const KNOWLEDGE_RELATION_TYPES = [
  "CITES",
  "AUTHORED_BY",
  "STUDIES",
  "USES_METHOD",
  "USES_DATASET",
  "USES_MODEL",
  "SUPPORTS",
  "CONTRADICTS",
  "EXTENDS",
  "AFFILIATED_WITH",
] as const;


export interface KnowledgeGraphPalette {
  paper: string;
  author: string;
  topic: string;
  method: string;
  dataset: string;
  model: string;
  finding: string;
  institution: string;
}


const DEFAULT_PALETTE: KnowledgeGraphPalette = {
  paper: "#2d6f95",
  author: "#5f8f70",
  topic: "#c9842a",
  method: "#8b6fb4",
  dataset: "#b85f6b",
  model: "#3f8f8a",
  finding: "#8d7b2f",
  institution: "#66758c",
};


export function nodeTypeLabel(nodeType: KnowledgeNodeType): string {
  return KNOWLEDGE_NODE_TYPES.find((item) => item.value === nodeType)?.label
    ?? nodeType;
}


export function knowledgeGraphEChartsSeries(
  data: KnowledgeGraphData,
  palette: KnowledgeGraphPalette = DEFAULT_PALETTE,
) {
  const categories = KNOWLEDGE_NODE_TYPES.map((item) => ({
    name: item.label,
  }));
  const categoryIndex = new Map(
    KNOWLEDGE_NODE_TYPES.map((item, index) => [item.value, index]),
  );
  return {
    data: data.nodes.map((node: KnowledgeGraphNode) => ({
      id: String(node.id),
      name: node.label,
      value: node.pagerank,
      category: categoryIndex.get(node.node_type) ?? 0,
      symbolSize: Math.min(
        62,
        16
          + Math.sqrt(Math.max(1, node.degree)) * 8
          + (node.is_core ? 10 : 0),
      ),
      itemStyle: {
        color: palette[node.node_type],
        borderColor: node.is_core ? "#ffffff" : "transparent",
        borderWidth: node.is_core ? 3 : 0,
      },
      raw: node,
    })),
    links: data.edges.map((edge) => ({
      id: String(edge.id),
      source: String(edge.source),
      target: String(edge.target),
      name: edge.relation_type,
      value: edge.confidence,
      relation_type: edge.relation_type,
      raw: edge,
    })),
    categories,
  };
}


export function evidenceRoute(
  edge: { paper_id: number; page_number: number },
) {
  return `/papers/${edge.paper_id}?page=${Math.max(1, edge.page_number)}`;
}
