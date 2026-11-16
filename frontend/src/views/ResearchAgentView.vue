<template>
  <div class="agent-page">
    <section class="hero page-card">
      <div>
        <p class="eyebrow">V0.4.0 EARLY PREVIEW · PERSISTENT RESEARCH AGENT</p>
        <h2>从研究目标到可核验报告</h2>
        <p class="muted">
          Agent 会持久化计划、按依赖调度只读工具，并依据中间检索结果触发一次受控重规划。
          外部 arXiv 摘要不会被标注为全文页码证据。
        </p>
      </div>
      <div class="dimensions">
        <el-tag effect="plain" type="warning">v0.4.0 提前预览</el-tag>
        <el-tag effect="plain" type="success">只读工具白名单</el-tag>
      </div>
    </section>

    <section class="goal-card page-card">
      <el-input
        v-model="goal"
        type="textarea"
        :rows="4"
        maxlength="2000"
        show-word-limit
        placeholder="例如：比较图神经网络与 Transformer 在网络流量预测中的方法、实验结论与局限"
        @keydown.ctrl.enter="createRun"
      />
      <div class="goal-actions">
        <span class="muted">
          预算：8 节点 / 12 次工具调用 / 3 次 arXiv 请求 / 180 秒
        </span>
        <el-button
          type="primary"
          :loading="creating"
          :disabled="goal.trim().length < 5"
          @click="createRun"
        >
          启动自主研究
        </el-button>
      </div>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
      />
    </section>

    <template v-if="run">
      <section class="overview page-card">
        <div class="overview-main">
          <div class="status-line">
            <el-tag :type="statusType(run.status)">{{ statusLabel(run.status) }}</el-tag>
            <span class="run-id mono">{{ run.run_id }}</span>
          </div>
          <h3>{{ run.topic || "正在理解研究目标…" }}</h3>
          <p class="muted">{{ run.research_question || run.goal }}</p>
          <div class="dimensions">
            <el-tag
              v-for="dimension in run.analysis_dimensions"
              :key="dimension"
              effect="plain"
            >
              {{ dimension }}
            </el-tag>
          </div>
        </div>
        <div class="metrics">
          <div><strong>{{ run.usage.tool_calls }}</strong><span>工具调用</span></div>
          <div><strong>{{ run.usage.llm_calls }}</strong><span>LLM 调用</span></div>
          <div><strong>{{ run.usage.arxiv_requests }}</strong><span>arXiv 请求</span></div>
          <div><strong>{{ run.replan_count }}</strong><span>动态重规划</span></div>
        </div>
        <el-button
          v-if="canCancel"
          type="danger"
          plain
          :loading="cancelling"
          @click="cancelRun"
        >
          取消任务
        </el-button>
      </section>

      <el-alert
        v-if="run.error"
        :title="run.error"
        type="error"
        show-icon
        :closable="false"
      />

      <section class="grid">
        <div class="page-card panel">
          <div class="panel-title">
            <div>
              <h3>持久化研究计划</h3>
              <p class="muted">依赖已满足的节点才会启动。</p>
            </div>
            <el-tag effect="plain">计划 v{{ run.plan_version || 0 }}</el-tag>
          </div>
          <div v-if="run.nodes.length" class="node-list">
            <article
              v-for="node in run.nodes"
              :key="node.id"
              class="node"
              :class="node.status"
            >
              <div class="node-head">
                <div>
                  <span class="node-id mono">{{ node.node_key }}</span>
                  <strong>{{ node.tool_name }}</strong>
                </div>
                <el-tag size="small" :type="statusType(node.status)">
                  {{ statusLabel(node.status) }}
                </el-tag>
              </div>
              <p>{{ node.reason }}</p>
              <div class="node-meta">
                <span>依赖：{{ node.depends_on.join(", ") || "无" }}</span>
                <span>重试：{{ node.retry_count }} / {{ node.retry_limit }}</span>
              </div>
              <p v-if="node.error" class="node-error">{{ node.error }}</p>
            </article>
          </div>
          <el-empty v-else description="Planner 正在生成计划" :image-size="70" />
        </div>

        <div class="page-card panel">
          <div class="panel-title">
            <div>
              <h3>运行时证据</h3>
              <p class="muted">每条本地证据都由 SQLite 校验论文、页码和 Chunk。</p>
            </div>
            <el-tag effect="plain">{{ validEvidence.length }} 条有效</el-tag>
          </div>
          <div v-if="run.evidence.length" class="evidence-list">
            <article
              v-for="item in run.evidence"
              :key="item.id"
              class="evidence"
              :class="{ invalid: !item.is_valid }"
            >
              <div class="evidence-head">
                <el-tag
                  size="small"
                  :type="item.source_type === 'local_full_text' ? 'success' : 'warning'"
                >
                  {{ evidenceLabel(item.source_type) }}
                </el-tag>
                <span class="mono">{{ evidenceLocator(item) }}</span>
              </div>
              <strong>{{ item.title || item.arxiv_id }}</strong>
              <p>{{ item.excerpt }}</p>
              <p v-if="item.validation_error" class="node-error">
                {{ item.validation_error }}
              </p>
            </article>
          </div>
          <el-empty v-else description="等待工具返回证据" :image-size="70" />
        </div>
      </section>

      <section class="page-card panel">
        <div class="panel-title">
          <div>
            <h3>工具调用与执行理由</h3>
            <p class="muted">失败调用、重试次数和耗时均持久化。</p>
          </div>
        </div>
        <el-table :data="run.tool_calls" size="small">
          <el-table-column prop="tool_name" label="工具" min-width="170" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="attempt" label="尝试" width="70" />
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              {{ row.duration_ms == null ? "—" : `${row.duration_ms} ms` }}
            </template>
          </el-table-column>
          <el-table-column label="结果摘要" min-width="260">
            <template #default="{ row }">
              <span class="mono summary">
                {{ row.result_summary ? JSON.stringify(row.result_summary) : row.error || "执行中" }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="执行理由" min-width="320">
            <template #default="{ row }">
              {{ reasonForCall(row.node_id) }}
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section v-if="report" class="page-card report">
        <div class="panel-title">
          <div>
            <p class="eyebrow">FINAL ARTIFACT</p>
            <h3>{{ report.report.report?.title }}</h3>
          </div>
          <el-tag type="success">{{ report.valid_source_count }} 条有效来源</el-tag>
        </div>
        <p class="summary-text">{{ report.report.report?.executive_summary }}</p>
        <article
          v-for="section in report.report.report?.sections || []"
          :key="section.heading"
          class="report-section"
        >
          <h4>{{ section.heading }}</h4>
          <p>{{ section.content }}</p>
          <span class="mono">{{ section.citation_ids.join(", ") }}</span>
        </article>
        <div class="limitations">
          <strong>局限</strong>
          <p>{{ report.report.report?.limitations }}</p>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { ApiError } from "@/api/http";
import {
  cancelResearchAgentRun,
  createResearchAgentRun,
  fetchResearchAgentReport,
  fetchResearchAgentRun,
  type ResearchAgentBudget,
  type ResearchAgentEvidence,
  type ResearchAgentReport,
  type ResearchAgentRun,
} from "@/api/researchAgent";

const defaultBudget: ResearchAgentBudget = {
  max_nodes: 8,
  max_tool_calls: 12,
  max_llm_calls: 4,
  max_arxiv_requests: 3,
  max_execution_seconds: 180,
};

const goal = ref("");
const run = ref<ResearchAgentRun | null>(null);
const report = ref<ResearchAgentReport | null>(null);
const creating = ref(false);
const cancelling = ref(false);
const error = ref("");
let pollTimer: ReturnType<typeof setTimeout> | null = null;

const canCancel = computed(
  () => !!run.value && ["pending", "running", "cancelling"].includes(run.value.status),
);
const validEvidence = computed(
  () => run.value?.evidence.filter((item) => item.is_valid) ?? [],
);

function statusLabel(status: string): string {
  return (
    {
      pending: "待调度",
      running: "执行中",
      retry: "重试中",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
      cancelling: "取消中",
    }[status] || status
  );
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "running" || status === "retry") return "warning";
  if (status === "cancelled") return "info";
  return "info";
}

function evidenceLabel(sourceType: string): string {
  return sourceType === "local_full_text"
    ? "本地全文"
    : "arXiv 外部摘要";
}

function evidenceLocator(item: ResearchAgentEvidence): string {
  if (item.source_type === "local_full_text") {
    return `paper ${item.paper_id} · p.${item.page_number} · chunk ${item.chunk_id}`;
  }
  return `arXiv:${item.arxiv_id} · 无全文页码`;
}

function reasonForCall(nodeId: string): string {
  return run.value?.nodes.find((node) => node.id === nodeId)?.reason || "—";
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function loadReport(runId: string) {
  try {
    report.value = await fetchResearchAgentReport(runId);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "报告读取失败。";
  }
}

async function poll(runId: string) {
  stopPolling();
  try {
    run.value = await fetchResearchAgentRun(runId);
    if (run.value.status === "completed") {
      await loadReport(runId);
      return;
    }
    if (["failed", "cancelled"].includes(run.value.status)) return;
    pollTimer = setTimeout(() => poll(runId), 1500);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "任务状态读取失败。";
    pollTimer = setTimeout(() => poll(runId), 2500);
  }
}

async function createRun() {
  if (goal.value.trim().length < 5 || creating.value) return;
  creating.value = true;
  error.value = "";
  report.value = null;
  stopPolling();
  try {
    run.value = await createResearchAgentRun(goal.value.trim(), defaultBudget);
    await poll(run.value.run_id);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "研究任务创建失败。";
  } finally {
    creating.value = false;
  }
}

async function cancelRun() {
  if (!run.value || cancelling.value) return;
  cancelling.value = true;
  try {
    run.value = await cancelResearchAgentRun(run.value.run_id);
    stopPolling();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "取消任务失败。";
  } finally {
    cancelling.value = false;
  }
}

onUnmounted(stopPolling);
</script>

<style scoped>
.agent-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.hero,
.goal-card,
.overview,
.panel,
.report {
  padding: 22px;
}

.hero,
.overview,
.panel-title {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: flex-start;
}

.hero h2,
.panel-title h3,
.report h3 {
  margin: 0 0 8px;
}

.eyebrow {
  margin: 0 0 8px;
  color: var(--accent-2);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.14em;
}

.goal-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.goal-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}

.overview {
  align-items: center;
}

.overview-main {
  flex: 1;
  min-width: 0;
}

.status-line,
.dimensions,
.node-head,
.node-meta,
.evidence-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.run-id {
  color: var(--muted);
  font-size: 12px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(70px, 1fr));
  gap: 10px;
}

.metrics div {
  padding: 10px 12px;
  border-radius: 12px;
  background: #f4f8fa;
  text-align: center;
}

.metrics strong,
.metrics span {
  display: block;
}

.metrics span {
  color: var(--muted);
  font-size: 11px;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}

.node-list,
.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
}

.node,
.evidence {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 13px;
  background: #fbfcfd;
}

.node.running {
  border-color: #d5a558;
  background: #fffaf1;
}

.node.completed {
  border-color: #bfdacb;
}

.node.failed,
.evidence.invalid {
  border-color: #e4b5af;
  background: #fff8f7;
}

.node p,
.evidence p,
.report-section p,
.limitations p {
  line-height: 1.65;
}

.node-meta {
  color: var(--muted);
  font-size: 12px;
}

.node-error {
  color: var(--danger);
}

.evidence-head {
  justify-content: space-between;
  font-size: 12px;
}

.summary {
  font-size: 12px;
  white-space: normal;
  word-break: break-word;
}

.report {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.summary-text {
  padding: 14px;
  border-left: 4px solid var(--accent-2);
  background: #fbf7f0;
  line-height: 1.7;
}

.report-section {
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.limitations {
  padding: 14px;
  border-radius: 12px;
  background: #f4f8fa;
}

@media (max-width: 980px) {
  .grid {
    grid-template-columns: 1fr;
  }

  .overview {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
