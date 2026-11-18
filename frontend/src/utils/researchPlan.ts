import type {
  ResearchAgentPlan,
  ResearchAgentPlanNode,
} from "@/api/researchAgent";


export interface EditablePlanNode {
  id: string;
  tool_name: string;
  argumentsText: string;
  depends_on: string[];
  reason: string;
  completion_condition: string;
  retry_limit: number;
  failure_strategy: "retry_then_fail" | "skip" | "fallback_tool";
  critical: boolean;
  fallback_tool: string | null;
  fallback_arguments_text: string;
  input_bindings: ResearchAgentPlanNode["input_bindings"];
  output_definition: ResearchAgentPlanNode["output_definition"];
}


export function planNodesToEditable(
  plan: ResearchAgentPlan | null,
): EditablePlanNode[] {
  return (plan?.nodes ?? []).map((node) => ({
    id: node.id,
    tool_name: node.tool_name,
    argumentsText: JSON.stringify(node.arguments, null, 2),
    depends_on: [...node.depends_on],
    reason: node.reason,
    completion_condition: node.completion_condition,
    retry_limit: node.retry_limit,
    failure_strategy: node.failure_strategy,
    critical: node.critical,
    fallback_tool: node.fallback_tool,
    fallback_arguments_text: JSON.stringify(node.fallback_arguments, null, 2),
    input_bindings: { ...node.input_bindings },
    output_definition: {
      description: node.output_definition.description,
      fields: [...node.output_definition.fields],
    },
  }));
}


export function parseNodeArguments(node: EditablePlanNode): Record<string, unknown> {
  return parseObjectJson(node.argumentsText, `节点 ${node.id} 的参数`);
}


function parseObjectJson(text: string, label: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text || "{}");
  } catch {
    throw new Error(`${label}不是合法 JSON。`);
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${label}必须是 JSON 对象。`);
  }
  return parsed as Record<string, unknown>;
}


export function editableNodesToPlan(
  nodes: EditablePlanNode[],
): ResearchAgentPlanNode[] {
  if (!nodes.length) throw new Error("研究计划至少需要一个节点。");
  const ids = nodes.map((node) => node.id.trim());
  if (ids.some((id) => !/^[A-Za-z][A-Za-z0-9_-]*$/.test(id))) {
    throw new Error("节点 ID 必须以字母开头，且只能包含字母、数字、下划线和连字符。");
  }
  if (new Set(ids).size !== ids.length) {
    throw new Error("节点 ID 不能重复。");
  }
  const known = new Set(ids);
  for (const node of nodes) {
    if (!node.tool_name.trim()) {
      throw new Error(`节点 ${node.id} 尚未选择工具。`);
    }
    if (node.depends_on.some((dependency) => !known.has(dependency))) {
      throw new Error(`节点 ${node.id} 依赖了不存在的节点。`);
    }
    if (node.depends_on.includes(node.id)) {
      throw new Error(`节点 ${node.id} 不能依赖自身。`);
    }
    if (node.failure_strategy === "skip" && node.critical) {
      throw new Error(`关键节点 ${node.id} 不能使用 skip 策略。`);
    }
    if (node.failure_strategy === "fallback_tool" && !node.fallback_tool) {
      throw new Error(`节点 ${node.id} 尚未选择替代工具。`);
    }
  }
  return nodes.map((node) => ({
    id: node.id.trim(),
    tool_name: node.tool_name,
    arguments: parseNodeArguments(node),
    depends_on: [...new Set(node.depends_on)],
    reason: node.reason.trim(),
    completion_condition: node.completion_condition.trim(),
    retry_limit: node.retry_limit,
    failure_strategy: node.failure_strategy,
    critical: node.critical,
    fallback_tool: node.failure_strategy === "fallback_tool"
      ? node.fallback_tool
      : null,
    fallback_arguments: node.failure_strategy === "fallback_tool"
      ? parseObjectJson(node.fallback_arguments_text, `节点 ${node.id} 的替代工具参数`)
      : {},
    input_bindings: { ...node.input_bindings },
    output_definition: {
      description: node.output_definition.description.trim(),
      fields: node.output_definition.fields
        .map((field) => field.trim())
        .filter(Boolean),
    },
  }));
}


export function createEditableNode(
  toolName: string,
  index: number,
): EditablePlanNode {
  return {
    id: `node_${index}`,
    tool_name: toolName,
    argumentsText: "{}",
    depends_on: [],
    reason: "补充用户指定的研究步骤。",
    completion_condition: "工具返回符合 Schema 的结构化结果。",
    retry_limit: 0,
    failure_strategy: "retry_then_fail",
    critical: true,
    fallback_tool: null,
    fallback_arguments_text: "{}",
    input_bindings: {},
    output_definition: {
      description: "",
      fields: [],
    },
  };
}
