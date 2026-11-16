import { http } from "./http";

export interface ResearchAgentBudget {
  max_nodes: number;
  max_tool_calls: number;
  max_llm_calls: number;
  max_arxiv_requests: number;
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
}

export interface ResearchAgentRun {
  run_id: string;
  goal: string;
  status: string;
  topic: string;
  research_question: string;
  time_range: string | null;
  analysis_dimensions: string[];
  plan:
    | {
        version: number;
        topic: string;
        research_question: string;
        nodes: ResearchAgentPlanNode[];
        generated_by: string;
      }
    | null;
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
  created_at: string | null;
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

export async function fetchResearchAgentRun(
  runId: string,
): Promise<ResearchAgentRun> {
  const response = await http.get<ResearchAgentRun>(
    `/research-agent/runs/${runId}`,
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
