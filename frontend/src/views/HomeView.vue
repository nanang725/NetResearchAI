<template>
  <div class="home-page">
    <section class="home-hero">
      <div class="hero-copy">
        <div class="hero-kicker">
          <span class="live-dot"></span>
          本地证据增强的科研工作台
        </div>
        <h2>{{ greeting }}，从一篇论文到下一项发现。</h2>
        <p>
          在真实馆藏、可回溯证据与 AI 研究流程之间保持连续。所有统计均来自当前工作区。
        </p>
      </div>

      <form class="quick-search" @submit.prevent="runQuickSearch">
        <label for="home-quick-search">快速检索本地论文证据</label>
        <div class="quick-search-control">
          <el-input
            id="home-quick-search"
            v-model="searchQuery"
            size="large"
            clearable
            placeholder="搜索主题、方法、结论或证据片段"
            :prefix-icon="Search"
            @keyup.enter="runQuickSearch"
          />
          <el-button
            type="primary"
            size="large"
            native-type="submit"
            :loading="searchLoading"
            :disabled="!searchQuery.trim()"
          >
            检索
          </el-button>
        </div>
        <div class="search-suggestions" aria-label="检索建议">
          <span>试试：</span>
          <button
            v-for="suggestion in suggestions"
            :key="suggestion"
            type="button"
            @click="useSuggestion(suggestion)"
          >
            {{ suggestion }}
          </button>
        </div>
      </form>
    </section>

    <div v-if="loadError" class="inline-notice error" role="alert">
      <Warning />
      <span>{{ loadError }}</span>
      <button type="button" @click="loadDashboard">重试</button>
    </div>

    <section class="stats-grid" aria-label="工作区概览">
      <article v-for="stat in stats" :key="stat.label" class="stat-card">
        <div class="stat-icon" :class="stat.tone">
          <component :is="stat.icon" />
        </div>
        <div>
          <span>{{ stat.label }}</span>
          <strong v-if="!dashboardLoading">{{ stat.value }}</strong>
          <span v-else class="skeleton-value"></span>
          <small>{{ stat.hint }}</small>
        </div>
      </article>
    </section>

    <section v-if="hasSearched" class="search-results page-card" aria-live="polite">
      <div class="section-heading">
        <div>
          <p class="eyebrow">QUICK EVIDENCE SEARCH</p>
          <h3>“{{ lastSearchedQuery }}”的证据命中</h3>
        </div>
        <span class="result-count">{{ searchResults.length }} 条结果</span>
      </div>

      <div v-if="searchError" class="inline-notice error compact">
        <Warning />
        <span>{{ searchError }}</span>
        <button type="button" @click="runQuickSearch">重试</button>
      </div>
      <div v-else-if="searchLoading" class="result-skeletons">
        <el-skeleton :rows="2" animated />
        <el-skeleton :rows="2" animated />
      </div>
      <el-empty
        v-else-if="!searchResults.length"
        description="没有检索到匹配的本地证据。文献库不会用模拟结果补足。"
      />
      <div v-else class="evidence-results">
        <button
          v-for="result in searchResults.slice(0, 4)"
          :key="result.chunk_id"
          type="button"
          class="evidence-result"
          @click="openEvidence(result)"
        >
          <div class="evidence-meta">
            <span>{{ result.paper_title }}</span>
            <el-tag size="small" effect="plain">第 {{ result.page_number }} 页</el-tag>
          </div>
          <p>{{ result.text }}</p>
          <span class="open-evidence">打开原文 <ArrowRight /></span>
        </button>
      </div>
    </section>

    <div class="dashboard-grid">
      <section class="workspace-panel page-card">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">LIBRARY</p>
            <h3>最近加入的文献</h3>
          </div>
          <RouterLink to="/library" class="text-link">查看论文库 <ArrowRight /></RouterLink>
        </div>

        <div v-if="dashboardLoading" class="panel-loading">
          <el-skeleton :rows="5" animated />
        </div>
        <el-empty
          v-else-if="!recentPapers.length"
          description="论文库还是空的。导入 PDF 后，最近文献会出现在这里。"
        >
          <el-button type="primary" @click="goUpload">导入第一篇论文</el-button>
        </el-empty>
        <div v-else class="paper-list">
          <button
            v-for="paper in recentPapers"
            :key="paper.id"
            type="button"
            class="paper-row"
            @click="openPaper(paper.id)"
          >
            <span class="paper-file-icon"><Document /></span>
            <span class="paper-main">
              <strong>{{ paper.title }}</strong>
              <small>
                {{ paper.authors || "作者元数据缺失" }}
                · {{ formatDateTime(paper.created_at) }}
              </small>
            </span>
            <span class="paper-meta">
              <el-tag
                :type="paper.index_status === 'indexed' ? 'success' : 'info'"
                size="small"
                effect="plain"
              >
                {{ statusLabel(paper.index_status) }}
              </el-tag>
              <small>{{ paper.page_count }} 页</small>
            </span>
          </button>
        </div>
      </section>

      <section class="workspace-panel page-card">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">TASKS</p>
            <h3>最近处理任务</h3>
          </div>
          <RouterLink to="/library" class="text-link">任务详情 <ArrowRight /></RouterLink>
        </div>

        <div v-if="dashboardLoading" class="panel-loading">
          <el-skeleton :rows="5" animated />
        </div>
        <el-empty
          v-else-if="!recentTasks.length"
          description="还没有 PDF 导入或索引修复任务。"
        />
        <div v-else class="task-list">
          <button
            v-for="task in recentTasks"
            :key="task.id"
            type="button"
            class="task-row"
            @click="goToLibrary"
          >
            <span class="task-status" :class="taskStatusClass(task.status)"></span>
            <span class="task-main">
              <strong>{{ taskTitle(task) }}</strong>
              <small>
                {{ statusLabel(task.status) }} ·
                {{ formatDateTime(task.updated_at || task.created_at) }}
              </small>
              <el-progress
                v-if="['queued', 'running'].includes(task.status)"
                :percentage="task.progress"
                :show-text="false"
                :stroke-width="4"
              />
            </span>
            <span class="task-count">
              {{ task.completed_count }}/{{ task.total_count }}
              <small>完成</small>
            </span>
          </button>
        </div>
      </section>
    </div>

    <section class="feature-section">
      <div class="feature-heading">
        <div>
          <p class="eyebrow">RESEARCH FLOW</p>
          <h3>常用研究入口</h3>
        </div>
        <p>从发现、精读、问答到写作，沿用现有真实数据与证据链路。</p>
      </div>
      <div class="feature-grid">
        <RouterLink
          v-for="feature in features"
          :key="feature.to"
          :to="feature.to"
          class="feature-card"
        >
          <span class="feature-icon"><component :is="feature.icon" /></span>
          <span class="feature-copy">
            <strong>
              {{ feature.title }}
              <el-tag v-if="feature.badge" size="small" effect="plain">
                {{ feature.badge }}
              </el-tag>
            </strong>
            <small>{{ feature.description }}</small>
          </span>
          <ArrowRight class="feature-arrow" />
        </RouterLink>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, type Component } from "vue";
import { useRouter } from "vue-router";
import {
  ArrowRight,
  ChatDotRound,
  Collection,
  Compass,
  Cpu,
  DataAnalysis,
  Document,
  EditPen,
  Files,
  Search,
  Share,
  Tickets,
  Warning,
} from "@element-plus/icons-vue";
import { fetchImportJobs, fetchPapers, searchPapers } from "@/api/papers";
import type { ImportJob, PaperListItem } from "@/types/paper";
import { formatDateTime, statusLabel } from "@/utils/format";

interface SearchEvidence {
  chunk_id: number;
  paper_id: number;
  paper_title: string;
  page_number: number;
  text: string;
}

interface FeatureLink {
  to: string;
  title: string;
  description: string;
  icon: Component;
  badge?: string;
}

const router = useRouter();
const papers = ref<PaperListItem[]>([]);
const jobs = ref<ImportJob[]>([]);
const dashboardLoading = ref(true);
const loadError = ref("");
const searchQuery = ref("");
const lastSearchedQuery = ref("");
const searchLoading = ref(false);
const searchError = ref("");
const hasSearched = ref(false);
const searchResults = ref<SearchEvidence[]>([]);

const suggestions = ["网络流量", "retrieval augmented generation", "Transformer"];

const greeting = computed(() => {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 12) return "早上好";
  if (hour < 18) return "下午好";
  return "晚上好";
});

const recentPapers = computed(() =>
  [...papers.value]
    .sort((left, right) => timestamp(right.created_at) - timestamp(left.created_at))
    .slice(0, 5),
);

const recentTasks = computed(() =>
  [...jobs.value]
    .sort((left, right) =>
      timestamp(right.updated_at || right.created_at)
      - timestamp(left.updated_at || left.created_at),
    )
    .slice(0, 5),
);

const indexedPaperCount = computed(() =>
  papers.value.filter((paper) => paper.index_status === "indexed").length,
);

const activeTaskCount = computed(() =>
  jobs.value.filter((job) => ["queued", "running"].includes(job.status)).length,
);

const stats = computed(() => [
  {
    label: "馆藏论文",
    value: compactNumber(papers.value.length),
    hint: "当前工作区真实收录",
    icon: Collection,
    tone: "primary",
  },
  {
    label: "已索引论文",
    value: compactNumber(indexedPaperCount.value),
    hint: "可用于证据检索与问答",
    icon: Tickets,
    tone: "success",
  },
  {
    label: "解析页数",
    value: compactNumber(papers.value.reduce((total, paper) => total + paper.page_count, 0)),
    hint: "来自已持久化 PDF 页面",
    icon: Document,
    tone: "info",
  },
  {
    label: "处理中任务",
    value: compactNumber(activeTaskCount.value),
    hint: activeTaskCount.value ? "解析或索引正在进行" : "当前没有活跃任务",
    icon: DataAnalysis,
    tone: activeTaskCount.value ? "warning" : "neutral",
  },
]);

const features: FeatureLink[] = [
  {
    to: "/discovery",
    title: "文献发现",
    description: "检索、收藏并导入 arXiv 官方结果",
    icon: Compass,
  },
  {
    to: "/search",
    title: "智能检索",
    description: "定位本地论文中的原文证据",
    icon: Search,
  },
  {
    to: "/qa",
    title: "智能问答",
    description: "基于馆藏上下文回答并回溯证据",
    icon: ChatDotRound,
  },
  {
    to: "/review",
    title: "多文献综述",
    description: "比较共同观点、差异与研究空白",
    icon: Files,
  },
  {
    to: "/writing",
    title: "学术写作",
    description: "大纲、References 与真实数据图表",
    icon: EditPen,
  },
  {
    to: "/knowledge-graph",
    title: "科研知识图谱",
    description: "实体、关系、方法比较与研究空白",
    icon: Share,
    badge: "v0.5.0",
  },
  {
    to: "/research-agent",
    title: "自主科研 Agent",
    description: "查看计划、工具轨迹与运行时证据",
    icon: Cpu,
    badge: "v0.4.0 稳定",
  },
];

function timestamp(value: string | null): number {
  if (!value) return 0;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    notation: value >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}

function taskTitle(task: ImportJob): string {
  return task.kind === "index_rebuild"
    ? "向量索引审计与修复"
    : `PDF 导入 · ${task.total_count} 个文件`;
}

function taskStatusClass(status: string): string {
  if (status === "completed") return "success";
  if (status === "failed" || status === "partial") return "danger";
  if (status === "running") return "warning";
  return "neutral";
}

async function loadDashboard(): Promise<void> {
  dashboardLoading.value = true;
  loadError.value = "";
  const [paperResult, jobResult] = await Promise.allSettled([
    fetchPapers(),
    fetchImportJobs(),
  ]);

  if (paperResult.status === "fulfilled") {
    papers.value = paperResult.value;
  }
  if (jobResult.status === "fulfilled") {
    jobs.value = jobResult.value;
  }
  if (paperResult.status === "rejected" || jobResult.status === "rejected") {
    loadError.value = "部分工作区数据暂时无法读取；其余真实数据仍可继续使用。";
  }
  dashboardLoading.value = false;
}

function extractSearchResults(payload: unknown): SearchEvidence[] {
  if (Array.isArray(payload)) return payload as SearchEvidence[];
  if (
    payload
    && typeof payload === "object"
    && Array.isArray((payload as { data?: unknown }).data)
  ) {
    return (payload as { data: SearchEvidence[] }).data;
  }
  return [];
}

async function runQuickSearch(): Promise<void> {
  const query = searchQuery.value.trim();
  if (!query || searchLoading.value) return;
  hasSearched.value = true;
  lastSearchedQuery.value = query;
  searchLoading.value = true;
  searchError.value = "";
  try {
    const response = await searchPapers(query);
    searchResults.value = extractSearchResults(response.data);
  } catch (error) {
    searchResults.value = [];
    searchError.value = error instanceof Error ? error.message : "本地证据检索失败。";
  } finally {
    searchLoading.value = false;
  }
}

function useSuggestion(value: string): void {
  searchQuery.value = value;
  void runQuickSearch();
}

function openEvidence(result: SearchEvidence): void {
  router.push({
    name: "paper-detail",
    params: { id: result.paper_id },
    query: { page: String(result.page_number) },
  });
}

function openPaper(id: number): void {
  router.push({ name: "paper-detail", params: { id } });
}

function goUpload(): void {
  router.push({ name: "library", query: { upload: "1" } });
}

function goToLibrary(): void {
  void router.push({ name: "library" });
}

onMounted(() => {
  void loadDashboard();
});
</script>

<style scoped>
.home-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.home-hero {
  position: relative;
  overflow: hidden;
  padding: 38px 40px 34px;
  color: var(--color-text-primary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xs);
}

.home-hero::after {
  position: absolute;
  top: -90px;
  right: -70px;
  width: 290px;
  height: 290px;
  content: "";
  pointer-events: none;
  border: 1px solid var(--color-border-subtle);
  border-radius: 50%;
  box-shadow:
    0 0 0 42px color-mix(in srgb, var(--color-border-subtle) 55%, transparent),
    0 0 0 92px color-mix(in srgb, var(--color-border-subtle) 28%, transparent);
}

.hero-copy,
.quick-search {
  position: relative;
  z-index: 1;
}

.hero-kicker {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-success);
  box-shadow: 0 0 0 4px var(--color-success-soft);
}

.hero-copy h2 {
  max-width: 760px;
  margin: 18px 0 12px;
  font-size: clamp(28px, 4vw, 44px);
  font-weight: 680;
  letter-spacing: -0.045em;
  line-height: 1.14;
}

.hero-copy > p {
  max-width: 720px;
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-lg);
  line-height: 1.7;
}

.quick-search {
  max-width: 880px;
  margin-top: 30px;
}

.quick-search > label {
  display: block;
  margin-bottom: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 650;
}

.quick-search-control {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: var(--space-2);
  padding: 5px;
  background: var(--color-surface-raised);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.quick-search-control :deep(.el-input__wrapper) {
  min-height: 44px;
  background: transparent;
  box-shadow: none;
}

.quick-search-control :deep(.el-button) {
  min-width: 88px;
  height: 44px;
}

.search-suggestions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
  color: var(--color-text-tertiary);
  font-size: var(--font-size-xs);
}

.search-suggestions button {
  padding: 3px 8px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  transition:
    color var(--duration-fast) var(--ease-standard),
    border-color var(--duration-fast) var(--ease-standard);
}

.search-suggestions button:hover {
  color: var(--color-text-primary);
  border-color: var(--color-border-default);
}

.inline-notice {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 11px 14px;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
}

.inline-notice svg {
  width: 16px;
  flex: 0 0 auto;
}

.inline-notice.error {
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border-color: color-mix(in srgb, var(--color-danger) 25%, transparent);
}

.inline-notice button {
  margin-left: auto;
  padding: 0;
  color: inherit;
  font-weight: 650;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.inline-notice.compact {
  margin-bottom: var(--space-3);
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-3);
}

.stat-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  min-width: 0;
  padding: 18px;
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-lg);
}

.stat-icon {
  display: grid;
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  place-items: center;
  color: var(--color-text-secondary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.stat-icon svg {
  width: 17px;
}

.stat-icon.primary {
  color: var(--color-primary);
  background: var(--color-primary-soft);
}

.stat-icon.success {
  color: var(--color-success);
  background: var(--color-success-soft);
}

.stat-icon.info {
  color: var(--color-info);
  background: var(--color-info-soft);
}

.stat-icon.warning {
  color: var(--color-warning);
  background: var(--color-warning-soft);
}

.stat-card > div:last-child {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.stat-card span {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.stat-card strong {
  margin: 2px 0;
  font-size: 25px;
  font-weight: 680;
  letter-spacing: -0.04em;
  line-height: 1.2;
}

.stat-card small {
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skeleton-value {
  width: 56px;
  height: 25px;
  margin: 4px 0;
  background: var(--color-bg-muted);
  border-radius: var(--radius-sm);
  animation: skeleton-pulse 1.4s ease-in-out infinite;
}

@keyframes skeleton-pulse {
  50% {
    opacity: 0.45;
  }
}

.search-results,
.workspace-panel {
  padding: var(--space-5);
}

.section-heading,
.panel-heading,
.feature-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}

.section-heading h3,
.panel-heading h3,
.feature-heading h3 {
  margin: 3px 0 0;
  font-size: var(--font-size-lg);
  font-weight: 650;
  letter-spacing: -0.015em;
}

.result-count {
  color: var(--color-text-tertiary);
  font-size: var(--font-size-xs);
}

.result-skeletons {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4);
  margin-top: var(--space-4);
}

.evidence-results {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-4);
}

.evidence-result {
  display: flex;
  min-width: 0;
  padding: var(--space-4);
  text-align: left;
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  flex-direction: column;
  transition:
    background var(--duration-fast) var(--ease-standard),
    border-color var(--duration-fast) var(--ease-standard);
}

.evidence-result:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-default);
}

.evidence-meta {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.evidence-meta > span {
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence-result p {
  display: -webkit-box;
  overflow: hidden;
  margin: 9px 0 12px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  line-height: 1.65;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.open-evidence {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: auto;
  color: var(--color-primary);
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.open-evidence svg,
.text-link svg,
.feature-arrow {
  width: 13px;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(330px, 0.85fr);
  gap: var(--space-5);
}

.text-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 550;
}

.text-link:hover {
  color: var(--color-primary);
}

.panel-loading {
  margin-top: var(--space-5);
}

.paper-list,
.task-list {
  margin-top: var(--space-3);
}

.paper-row,
.task-row {
  display: flex;
  width: 100%;
  min-width: 0;
  align-items: center;
  gap: var(--space-3);
  padding: 12px 8px;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-top: 1px solid var(--color-border-subtle);
  transition: background var(--duration-fast) var(--ease-standard);
}

.paper-row:hover,
.task-row:hover {
  background: var(--color-surface-hover);
}

.paper-row:first-child,
.task-row:first-child {
  border-top: 0;
}

.paper-file-icon {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  place-items: center;
  color: var(--color-text-secondary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-sm);
}

.paper-file-icon svg {
  width: 15px;
}

.paper-main,
.task-main {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}

.paper-main strong,
.task-main strong {
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  font-weight: 600;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.paper-main small,
.task-main small,
.paper-meta small {
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.paper-meta {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-end;
  gap: 4px;
  flex-direction: column;
}

.task-status {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--color-text-tertiary);
}

.task-status.success {
  background: var(--color-success);
}

.task-status.warning {
  background: var(--color-warning);
  box-shadow: 0 0 0 4px var(--color-warning-soft);
}

.task-status.danger {
  background: var(--color-danger);
}

.task-main :deep(.el-progress) {
  margin-top: 6px;
}

.task-count {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-end;
  color: var(--color-text-secondary);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  flex-direction: column;
}

.task-count small {
  color: var(--color-text-tertiary);
  font-family: var(--font-sans);
  font-size: 10px;
}

.feature-section {
  padding: var(--space-2) 0 var(--space-4);
}

.feature-heading {
  align-items: flex-end;
  padding: 0 var(--space-1) var(--space-4);
}

.feature-heading > p {
  max-width: 420px;
  margin: 0;
  color: var(--color-text-tertiary);
  font-size: var(--font-size-xs);
  text-align: right;
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
}

.feature-card {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-lg);
  transition:
    border-color var(--duration-fast) var(--ease-standard),
    transform var(--duration-fast) var(--ease-standard);
}

.feature-card:hover {
  border-color: var(--color-border-strong);
  transform: translateY(-1px);
}

.feature-icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  place-items: center;
  color: var(--color-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-md);
}

.feature-icon svg {
  width: 17px;
}

.feature-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}

.feature-copy strong {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-sm);
  font-weight: 650;
}

.feature-copy small {
  margin-top: 2px;
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.feature-arrow {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
}

@media (max-width: 1100px) {
  .stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .dashboard-grid {
    grid-template-columns: 1fr;
  }

  .feature-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .home-page {
    gap: var(--space-4);
  }

  .home-hero {
    padding: 26px 20px 22px;
  }

  .home-hero::after {
    display: none;
  }

  .hero-copy h2 {
    margin-top: 14px;
    font-size: 29px;
  }

  .hero-copy > p {
    font-size: var(--font-size-base);
  }

  .quick-search {
    margin-top: 22px;
  }

  .quick-search-control {
    grid-template-columns: 1fr;
  }

  .quick-search-control :deep(.el-button) {
    width: 100%;
  }

  .stats-grid,
  .feature-grid,
  .evidence-results,
  .result-skeletons {
    grid-template-columns: 1fr;
  }

  .stat-card {
    padding: 15px;
  }

  .search-results,
  .workspace-panel {
    padding: var(--space-4);
  }

  .paper-meta {
    display: none;
  }

  .feature-heading > p {
    display: none;
  }
}
</style>
