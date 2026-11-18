import { http } from "./http";

export interface ResearchAgentBudget {
  max_nodes: number;
  max_tool_calls: number;
  max_llm_calls: number;
  max_arxiv_requests: number;
  max_replans: number;
  max_execution_seconds: number;
}

export interface ResearchAgentPlanNode {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  depends_on: string[];
  reason: string;
  completion_condition: string;
  retry_limit: number;
  failure_strategy: "retry_then_fail" | "skip" | "fallback_tool";
  critical: boolean;
  fallback_tool: string | null;
  fallback_arguments: Record<string, unknown>;
  input_bindings: Record<
    string,
    { source_node_id: string; output_key: string; max_items?: number | null }
  >;
  output_definition: {
    description: string;
    fields: string[];
  };
}

export interface ResearchAgentPlan {
  version: number;
  topic: string;
  research_question: string;
  time_range: string | null;
  analysis_dimensions: string[];
  nodes: ResearchAgentPlanNode[];
  generated_by: "llm" | "fallback" | "controlled_replan";
  edited_by_user: boolean;
}

export interface ResearchAgentPlanHistoryEntry {
  version: number | null;
  action: string | null;
  change_reason: string | null;
  plan: ResearchAgentPlan | null;
  created_at: string | null;
}

export interface ResearchAgentToolCall {
  id: string;
  node_id: string;
  tool_name: string;
  attempt: number;
  arguments_summary: Record<string, unknown>;
  result_summary: Record<string, unknown> | null;
  status: string;
  duration_ms: number | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface ResearchAgentEvidence {
  id: string;
  node_id: string;
  source_type: "local_full_text" | "external_arxiv_abstract" | string;
  paper_id: number | null;
  page_number: number | null;
  chunk_id: number | null;
  arxiv_id: string | null;
  title: string | null;
  content_hash: string;
  excerpt: string;
  source_url: string | null;
  is_valid: boolean;
  validation_error: string | null;
}

export interface ResearchAgentNodeState {
  id: string;
  node_key: string;
  node_type: string;
  tool_name: string | null;
  input: Record<string, unknown>;
  depends_on: string[];
  reason: string;
  completion_condition: string;
  output_summary: Record<string, unknown> | null;
  status: string;
  retry_count: number;
  retry_limit: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  execution_input: {
    node_id: string;
    node_key: string;
    tool_name: string;
    original_arguments: Record<string, unknown>;
    resolved_arguments: Record<string, unknown> | null;
    binding_sources: Array<{
      argument: string;
      source_node_id: string;
      output_key: string;
      resolved_type: string;
      item_count: number | null;
    }>;
    error?: {
      category: string;
      code: string;
      message: string;
    } | null;
  } | null;
  structured_output: {
    node_id: string;
    node_key: string;
    tool_name: string;
    output: {
      success: boolean;
      tool_name: string;
      status: "success" | "empty_result" | "error";
      data: Record<string, unknown>;
      summary: Record<string, unknown>;
      warnings: string[];
      error: {
        category: string;
        code: string;
        message: string;
        retryable: boolean;
      } | null;
    };
  } | null;
  error_output: {
    node_id: string;
    node_key: string;
    tool_name: string;
    error: {
      category: string;
      code: string;
      message: string;
      retryable: boolean;
    };
  } | null;
  strategy_decision: {
    node_key: string;
    strategy: string;
    outcome: string;
    reason: string;
    fallback_tool: string | null;
    error_category: string | null;
  } | null;
}

export interface ResearchAgentRun {
  run_id: string;
  goal: string;
  status: string;
  topic: string;
  research_question: string;
  time_range: string | null;
  analysis_dimensions: string[];
  plan: ResearchAgentPlan | null;
  plan_estimate: {
    node_count: number;
    tool_call_estimate: number;
    arxiv_request_estimate: number;
    llm_call_estimate: number;
  } | null;
  plan_history: ResearchAgentPlanHistoryEntry[];
  plan_version: number;
  replan_count: number;
  budget: ResearchAgentBudget & { writer_concurrency: number };
  usage: {
    nodes: number;
    tool_calls: number;
    llm_calls: number;
    arxiv_requests: number;
    elapsed_seconds: number | null;
  };
  cancel_requested: boolean;
  error: string | null;
  nodes: ResearchAgentNodeState[];
  tool_calls: ResearchAgentToolCall[];
  evidence: ResearchAgentEvidence[];
  report_available: boolean;
  llm_diagnostics: Array<{
    stage: string;
    generation_mode?: string;
    attempts?: number | Array<Record<string, unknown>>;
    diagnostics?: Array<Record<string, unknown>> | Record<string, unknown>;
    created_at?: string | null;
    [key: string]: unknown;
  }>;
  worker_state: {
    lease_status: "active" | "expired" | "released";
    lease_generation: number;
    lease_expires_at: string | null;
    heartbeat_at: string | null;
    resume_count: number;
  };
  replan_decisions: Array<{
    trigger: string;
    old_plan_version: number;
    new_plan_version: number | null;
    reason: string;
    planner_source: string;
    added_nodes: string[];
    accepted: boolean;
    error_category?: string | null;
    created_at: string;
  }>;
  recoveries: Array<{
    reason: string;
    previous_status: string;
    generation: number;
    completed_nodes_reused: string[];
    unfinished_nodes: string[];
    created_at: string;
  }>;
  created_at: string | null;
}

export interface ResearchAgentPlanUpdate {
  goal: string;
  topic: string;
  research_question: string;
  time_range: string | null;
  analysis_dimensions: string[];
  nodes: ResearchAgentPlanNode[];
  budget: ResearchAgentBudget;
  change_reason: string;
}

export interface ResearchAgentToolCatalogItem {
  name: string;
  description: string;
  cost_class: "local" | "network" | "llm";
  max_attempts: number;
  max_calls_per_run: number;
  llm_calls_per_execution: number;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
}

export interface ResearchAgentReport {
  run_id: string;
  status: string;
  valid_source_count: number;
  report: {
    report?: {
      title: string;
      executive_summary: string;
      sections: Array<{
        heading: string;
        content: string;
        citation_ids: string[];
      }>;
      limitations: string;
    };
    markdown?: string;
    sources?: Array<Record<string, unknown>>;
    plan_version?: number;
    replan_count?: number;
    generation_mode?: "llm" | "fallback";
  };
  created_at: string | null;
}

export async function createResearchAgentRun(
  goal: string,
  budget: ResearchAgentBudget,
): Promise<ResearchAgentRun> {
  const response = await http.post<ResearchAgentRun>("/research-agent/runs", {
    goal,
    budget,
  });
  return response.data;
}

export async function createResearchAgentPlan(
  goal: string,
  budget: ResearchAgentBudget,
): Promise<ResearchAgentRun> {
  const response = await http.post<ResearchAgentRun>("/research-agent/plans", {
    goal,
    budget,
  });
  return response.data;
}

export async function fetchResearchAgentPlan(
  planId: string,
): Promise<ResearchAgentRun> {
  const response = await http.get<ResearchAgentRun>(
    `/research-agent/plans/${planId}`,
  );
  return response.data;
}

export async function updateResearchAgentPlan(
  planId: string,
  update: ResearchAgentPlanUpdate,
): Promise<ResearchAgentRun> {
  const response = await http.put<ResearchAgentRun>(
    `/research-agent/plans/${planId}`,
    update,
  );
  return response.data;
}

export async function confirmResearchAgentPlan(
  planId: string,
): Promise<ResearchAgentRun> {
  const response = await http.post<ResearchAgentRun>(
    `/research-agent/plans/${planId}/confirm`,
  );
  return response.data;
}

export async function fetchResearchAgentTools(): Promise<
  ResearchAgentToolCatalogItem[]
> {
  const response = await http.get<ResearchAgentToolCatalogItem[]>(
    "/research-agent/tools",
  );
  return response.data ?? [];
}

export async function fetchResearchAgentRun(
  runId: string,
): Promise<ResearchAgentRun> {
  const response = await http.get<ResearchAgentRun>(
    `/research-agent/runs/${runId}`,
  );
  return response.data;
}


export async function fetchLatestResearchAgentRun(): Promise<ResearchAgentRun> {
  const response = await http.get<ResearchAgentRun>(
    "/research-agent/runs/latest",
  );
  return response.data;
}


export async function cancelResearchAgentRun(
  runId: string,
): Promise<ResearchAgentRun> {
  const response = await http.post<ResearchAgentRun>(
    `/research-agent/runs/${runId}/cancel`,
  );
  return response.data;
}

export async function fetchResearchAgentReport(
  runId: string,
): Promise<ResearchAgentReport> {
  const response = await http.get<ResearchAgentReport>(
    `/research-agent/runs/${runId}/report`,
  );
  return response.data;
}
