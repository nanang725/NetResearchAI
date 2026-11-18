<template>
  <div class="search-page">
    <section class="search-hero page-card">
      <div>
        <p class="eyebrow">LOCAL EVIDENCE RETRIEVAL</p>
        <h2>智能文献检索</h2>
        <p class="muted">
          使用现有混合检索能力定位本地论文原文，结果保留论文、页码、Chunk 与真实检索来源。
        </p>
      </div>
      <el-tag effect="plain">本地馆藏 · 真实原文</el-tag>
    </section>

    <section class="search-panel page-card">
      <form class="query-form" @submit.prevent="handleSearch">
        <el-input
          v-model="query"
          size="large"
          clearable
          :prefix-icon="Search"
          placeholder="输入研究主题、方法、结论或原文关键词"
          @keyup.enter="handleSearch"
        />
        <el-button
          type="primary"
          size="large"
          native-type="submit"
          :loading="loading"
          :disabled="!query.trim()"
        >
          搜索证据
        </el-button>
      </form>
      <div class="filter-line">
        <button
          type="button"
          class="filter-toggle"
          :aria-expanded="showFilters"
          @click="showFilters = !showFilters"
        >
          {{ showFilters ? "收起结果筛选" : "结果筛选" }}
        </button>
        <span v-if="elapsedMs !== null" class="elapsed">
          本次检索实测 {{ elapsedMs }} ms
        </span>
      </div>
      <div v-if="showFilters" class="filter-grid">
        <el-select v-model="paperFilter" clearable placeholder="全部论文">
          <el-option
            v-for="paper in resultPapers"
            :key="paper.paper_id"
            :label="paper.paper_title"
            :value="paper.paper_id"
          />
        </el-select>
        <el-select v-model="sortMode">
          <el-option label="检索相关度顺序" value="relevance" />
          <el-option label="按论文标题" value="title" />
          <el-option label="按页码" value="page" />
        </el-select>
      </div>
      <div v-if="errorMessage" class="inline-error" role="alert">
        <span>{{ errorMessage }}</span>
        <el-button link type="primary" @click="handleSearch">重试</el-button>
      </div>
    </section>

    <section v-if="loading" class="loading-list" aria-live="polite">
      <div v-for="index in 3" :key="index" class="loading-card page-card">
        <el-skeleton :rows="4" animated />
      </div>
    </section>

    <template v-else-if="hasSearched">
      <section class="result-summary">
        <div>
          <p class="eyebrow">RETRIEVAL RESULTS</p>
          <h3>“{{ lastQuery }}”</h3>
        </div>
        <div class="summary-metrics">
          <span><strong>{{ filteredResults.length }}</strong> 条证据</span>
          <span><strong>{{ resultPapers.length }}</strong> 篇论文</span>
          <span v-if="elapsedMs !== null"><strong>{{ elapsedMs }}</strong> ms API 耗时</span>
        </div>
      </section>

      <el-empty
        v-if="!filteredResults.length"
        description="没有找到匹配的本地证据。检索不会返回模拟结果。"
      />
      <section v-else class="evidence-list">
        <article
          v-for="(item, index) in filteredResults"
          :key="item.chunk_id"
          class="evidence-card page-card"
        >
          <div class="evidence-index">{{ String(index + 1).padStart(2, "0") }}</div>
          <div class="evidence-main">
            <div class="evidence-heading">
              <div>
                <el-tag size="small" effect="plain">第 {{ item.page_number }} 页</el-tag>
                <el-tag
                  v-if="item.retrieval_method"
                  size="small"
                  effect="plain"
                  type="info"
                >
                  {{ retrievalMethodLabel(item.retrieval_method) }}
                </el-tag>
                <span class="mono">Chunk {{ item.chunk_id }}</span>
              </div>
              <span v-if="retrievalScore(item)" class="score">
                score {{ retrievalScore(item) }}
              </span>
            </div>
            <h4>{{ item.paper_title }}</h4>
            <p class="evidence-text">{{ item.text }}</p>
            <div class="evidence-actions">
              <span class="muted">
                {{ item.retrieval_sources?.join(" + ") || "本地混合检索" }}
              </span>
              <el-button link type="primary" @click="openEvidence(item)">
                打开原文与证据位置
              </el-button>
            </div>
          </div>
        </article>
      </section>
    </template>

    <section v-else class="guided-empty page-card">
      <div class="guided-icon"><Search /></div>
      <div>
        <h3>从一条可验证的证据开始</h3>
        <p>
          可尝试研究主题、方法名称、实验结论或论文中的原文短语。系统会返回当前已索引的真实文本块。
        </p>
        <div class="search-examples">
          <button
            v-for="example in examples"
            :key="example"
            type="button"
            @click="searchExample(example)"
          >
            {{ example }}
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Search } from "@element-plus/icons-vue";
import { searchPapers } from "@/api/papers";
import { ApiError } from "@/api/http";

interface SearchEvidence {
  chunk_id: number;
  paper_id: number;
  paper_title: string;
  page_number: number;
  text: string;
  retrieval_method?: string;
  retrieval_score?: number;
  retrieval_sources?: string[];
}

const route = useRoute();
const router = useRouter();
const query = ref("");
const loading = ref(false);
const hasSearched = ref(false);
const lastQuery = ref("");
const results = ref<SearchEvidence[]>([]);
const errorMessage = ref("");
const elapsedMs = ref<number | null>(null);
const showFilters = ref(false);
const paperFilter = ref<number | null>(null);
const sortMode = ref<"relevance" | "title" | "page">("relevance");
const examples = ["retrieval augmented generation", "网络流量", "Transformer"];

const resultPapers = computed(() => {
  const seen = new Map<number, { paper_id: number; paper_title: string }>();
  results.value.forEach((item) => {
    if (!seen.has(item.paper_id)) {
      seen.set(item.paper_id, {
        paper_id: item.paper_id,
        paper_title: item.paper_title,
      });
    }
  });
  return [...seen.values()];
});

const filteredResults = computed(() => {
  const filtered = paperFilter.value === null
    ? [...results.value]
    : results.value.filter((item) => item.paper_id === paperFilter.value);
  if (sortMode.value === "title") {
    return filtered.sort((left, right) =>
      left.paper_title.localeCompare(right.paper_title, "zh-CN"),
    );
  }
  if (sortMode.value === "page") {
    return filtered.sort(
      (left, right) =>
        left.paper_title.localeCompare(right.paper_title, "zh-CN")
        || left.page_number - right.page_number,
    );
  }
  return filtered;
});

function extractResults(payload: unknown): SearchEvidence[] {
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

function retrievalMethodLabel(method: string): string {
  return (
    {
      hybrid: "混合检索",
      bm25: "BM25",
      vector: "向量检索",
    }[method] || method
  );
}

function retrievalScore(item: SearchEvidence): string {
  const score = item.retrieval_score;
  return typeof score === "number" && Number.isFinite(score)
    ? score.toFixed(3)
    : "";
}

async function handleSearch() {
  const currentQuery = query.value.trim();
  if (!currentQuery || loading.value) {
    if (!currentQuery) ElMessage.warning("请输入搜索词。");
    return;
  }
  loading.value = true;
  hasSearched.value = true;
  lastQuery.value = currentQuery;
  errorMessage.value = "";
  elapsedMs.value = null;
  paperFilter.value = null;
  const startedAt = performance.now();
  try {
    const response = await searchPapers(currentQuery);
    results.value = extractResults(response.data);
  } catch (error) {
    results.value = [];
    errorMessage.value = error instanceof ApiError
      ? error.message
      : "检索失败，请检查后端服务。";
  } finally {
    elapsedMs.value = Math.round(performance.now() - startedAt);
    loading.value = false;
  }
}

function searchExample(example: string) {
  query.value = example;
  void handleSearch();
}

function openEvidence(item: SearchEvidence) {
  router.push({
    name: "paper-detail",
    params: { id: item.paper_id },
    query: { page: String(item.page_number) },
  });
}

onMounted(() => {
  const initialQuery = typeof route.query.q === "string" ? route.query.q : "";
  if (initialQuery.trim()) {
    query.value = initialQuery;
    void handleSearch();
  }
});
</script>

<style scoped>
.search-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1120px;
  margin: 0 auto;
}

.search-hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 24px;
}

.search-hero h2 {
  margin: 4px 0 5px;
  font-size: 23px;
}

.search-hero p {
  margin: 0;
}

.search-panel {
  padding: 20px;
}

.query-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.query-form :deep(.el-input__wrapper),
.query-form :deep(.el-button) {
  min-height: 44px;
}

.filter-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
}

.filter-toggle {
  padding: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.elapsed {
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 10px;
}

.filter-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 180px;
  gap: 10px;
  padding: 12px;
  margin-top: 10px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.inline-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  margin-top: 12px;
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border-radius: var(--radius-md);
  font-size: 13px;
}

.inline-error :deep(.el-button) {
  margin-left: auto;
}

.loading-list,
.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.loading-card {
  padding: 20px;
}

.result-summary {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 2px 2px 0;
}

.result-summary h3 {
  margin: 3px 0 0;
  font-size: 18px;
}

.summary-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.summary-metrics strong {
  color: var(--color-text-primary);
  font-family: var(--font-mono);
}

.evidence-card {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 12px;
  padding: 18px;
}

.evidence-index {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  color: var(--color-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 700;
}

.evidence-main {
  min-width: 0;
}

.evidence-heading,
.evidence-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.evidence-heading > div {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}

.evidence-heading .mono,
.score {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.evidence-card h4 {
  margin: 10px 0 8px;
  font-size: 15px;
  line-height: 1.5;
}

.evidence-text {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 6;
}

.evidence-actions {
  padding-top: 10px;
  margin-top: 10px;
  border-top: 1px solid var(--color-border-subtle);
  font-size: 10px;
}

.guided-empty {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 24px;
}

.guided-icon {
  display: grid;
  width: 42px;
  height: 42px;
  flex: 0 0 auto;
  place-items: center;
  color: var(--color-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-lg);
}

.guided-icon svg {
  width: 20px;
}

.guided-empty h3 {
  margin: 0 0 5px;
}

.guided-empty p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.7;
}

.search-examples {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 14px;
}

.search-examples button {
  padding: 5px 9px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  font-size: 11px;
}

.search-examples button:hover {
  color: var(--color-primary);
  border-color: var(--color-primary-border);
}

@media (max-width: 720px) {
  .search-hero,
  .result-summary,
  .evidence-actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .query-form,
  .filter-grid {
    grid-template-columns: 1fr;
  }

  .query-form :deep(.el-button) {
    width: 100%;
  }

  .evidence-card {
    grid-template-columns: 1fr;
  }

  .evidence-index {
    display: none;
  }
}
</style>
