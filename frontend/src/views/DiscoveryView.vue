<template>
  <div class="discovery-page">
    <section class="page-card discovery-hero">
      <div>
        <p class="eyebrow">MODULE 4 · ARXIV DISCOVERY</p>
        <h2>自动化文献搜索、追踪与推荐</h2>
        <p class="muted">
          搜索结果来自 arXiv 官方公开 API；收藏前会再次回查官方元数据。
          PDF 入库复用现有导入任务和 SHA 去重。
        </p>
      </div>
      <div class="hero-tags">
        <el-tag type="success">arXiv 官方 API</el-tag>
        <el-tag type="warning">外部/本地来源分离</el-tag>
        <el-tag>现有导入任务</el-tag>
      </div>
    </section>

    <el-tabs v-model="activeTab" class="page-card discovery-tabs">
      <el-tab-pane label="外部搜索" name="search">
        <div class="tab-body">
          <section class="search-panel">
            <el-form label-position="top">
              <div class="search-row">
                <el-form-item label="关键词" class="query-field">
                  <el-input
                    v-model="searchQuery"
                    clearable
                    placeholder="例如：retrieval augmented generation"
                    @keyup.enter="runSearch(1)"
                  />
                </el-form-item>
                <el-form-item label="检索字段">
                  <el-select v-model="searchField">
                    <el-option label="全部字段" value="all" />
                    <el-option label="标题" value="title" />
                    <el-option label="摘要" value="abstract" />
                    <el-option label="作者" value="author" />
                  </el-select>
                </el-form-item>
                <el-form-item label="排序">
                  <el-select v-model="searchSort">
                    <el-option label="相关度" value="relevance" />
                    <el-option label="最近更新" value="last_updated" />
                    <el-option label="提交日期" value="submitted" />
                  </el-select>
                </el-form-item>
                <el-form-item label="方向">
                  <el-select v-model="searchOrder">
                    <el-option label="降序" value="descending" />
                    <el-option label="升序" value="ascending" />
                  </el-select>
                </el-form-item>
              </div>
              <div class="search-actions">
                <span class="muted">每页 {{ searchPageSize }} 条</span>
                <el-button
                  type="primary"
                  :loading="searchLoading"
                  :disabled="searchQuery.trim().length < 2"
                  @click="runSearch(1)"
                >
                  搜索 arXiv
                </el-button>
              </div>
            </el-form>
            <p v-if="searchError" class="error-text">{{ searchError }}</p>
          </section>

          <template v-if="searchResult">
            <div class="result-toolbar">
              <div>
                <strong>{{ searchResult.total_results }}</strong>
                <span class="muted"> 条官方结果 · 第 {{ searchResult.page }} 页</span>
              </div>
              <el-tag type="success">来源：{{ searchResult.source }}</el-tag>
            </div>
            <el-empty
              v-if="!searchResult.results.length"
              description="没有找到匹配论文；系统不会用虚构结果补足"
            />
            <section class="external-list">
              <article
                v-for="paper in searchResult.results"
                :key="paper.arxiv_id"
                class="external-card"
              >
                <div class="card-head">
                  <div>
                    <div class="tag-row">
                      <el-tag
                        :type="paper.in_local_library ? 'success' : 'info'"
                        size="small"
                      >
                        {{ externalResultLabel(paper) }}
                      </el-tag>
                      <el-tag size="small">arXiv:{{ paper.arxiv_id }}</el-tag>
                      <el-tag v-if="paper.primary_category" size="small" type="warning">
                        {{ paper.primary_category }}
                      </el-tag>
                    </div>
                    <h3>{{ paper.title }}</h3>
                  </div>
                  <span class="date muted">{{ formatArxivDate(paper.published) }}</span>
                </div>
                <p class="authors">{{ paper.authors.join(", ") || "作者未知" }}</p>
                <p class="summary">{{ paper.summary }}</p>
                <div class="card-actions">
                  <a :href="paper.abs_url" target="_blank" rel="noreferrer">
                    <el-button size="small" plain>打开 arXiv</el-button>
                  </a>
                  <el-button
                    v-if="paper.local_paper_id"
                    size="small"
                    type="success"
                    @click="openLocalPaper(paper.local_paper_id)"
                  >
                    打开本地论文
                  </el-button>
                  <el-button
                    v-else
                    size="small"
                    type="primary"
                    :loading="favoritingId === paper.arxiv_id"
                    @click="favoritePaper(paper)"
                  >
                    {{ paper.favorite_id ? "已收藏" : "收藏" }}
                  </el-button>
                </div>
              </article>
            </section>
            <el-pagination
              v-if="searchResult.total_results > searchPageSize"
              background
              layout="prev, pager, next"
              :page-size="searchPageSize"
              :current-page="searchResult.page"
              :total="Math.min(searchResult.total_results, searchPageSize * 100)"
              @current-change="runSearch"
            />
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="收藏与入库" name="favorites" lazy>
        <div class="tab-body">
          <div class="section-toolbar">
            <div>
              <h3>外部文献收藏</h3>
              <p class="muted">下载失败会保留收藏记录并显示失败原因。</p>
            </div>
            <el-button :loading="favoritesLoading" @click="loadFavorites">刷新状态</el-button>
          </div>
          <p v-if="favoritesError" class="error-text">{{ favoritesError }}</p>
          <el-empty v-if="!favoritesLoading && !favorites.length" description="还没有收藏外部文献" />
          <section class="external-list">
            <article
              v-for="favorite in favorites"
              :key="favorite.favorite_id ?? favorite.arxiv_id"
              class="external-card"
            >
              <div class="card-head">
                <div>
                  <div class="tag-row">
                    <el-tag size="small">arXiv:{{ favorite.arxiv_id }}</el-tag>
                    <el-tag
                      :type="favoriteImportStatusType(favorite.import_status)"
                      size="small"
                    >
                      {{ favoriteImportStatusLabel(favorite.import_status) }}
                    </el-tag>
                  </div>
                  <h3>{{ favorite.title }}</h3>
                </div>
                <span class="date muted">{{ formatArxivDate(favorite.published) }}</span>
              </div>
              <p class="authors">{{ favorite.authors.join(", ") || "作者未知" }}</p>
              <p class="summary">{{ favorite.summary }}</p>
              <p v-if="favorite.import_error" class="error-text">
                入库失败：{{ favorite.import_error }}
              </p>
              <div v-if="favorite.import_job" class="job-progress">
                <span>导入任务 {{ favorite.import_job.id.slice(0, 8) }}</span>
                <el-progress :percentage="favorite.import_job.progress" />
              </div>
              <div class="card-actions">
                <a :href="favorite.abs_url" target="_blank" rel="noreferrer">
                  <el-button size="small" plain>打开 arXiv</el-button>
                </a>
                <el-button
                  v-if="favorite.local_paper_id"
                  size="small"
                  type="success"
                  @click="openLocalPaper(favorite.local_paper_id)"
                >
                  打开本地论文
                </el-button>
                <el-button
                  v-else
                  size="small"
                  type="primary"
                  :loading="importingFavoriteId === favorite.favorite_id"
                  :disabled="['queued', 'running'].includes(favorite.import_status)"
                  @click="importFavorite(favorite)"
                >
                  下载 PDF 并入库
                </el-button>
              </div>
            </article>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="关键词追踪" name="tracking" lazy>
        <div class="tab-body">
          <section class="tracking-form">
            <h3>保存 arXiv 追踪条件</h3>
            <el-form label-position="top">
              <div class="search-row">
                <el-form-item label="任务名称">
                  <el-input v-model="trackingName" placeholder="例如：RAG 最新论文" />
                </el-form-item>
                <el-form-item label="追踪关键词" class="query-field">
                  <el-input v-model="trackingQuery" placeholder="关键词或短语" />
                </el-form-item>
                <el-form-item label="检索字段">
                  <el-select v-model="trackingField">
                    <el-option label="全部字段" value="all" />
                    <el-option label="标题" value="title" />
                    <el-option label="摘要" value="abstract" />
                  </el-select>
                </el-form-item>
              </div>
              <el-button type="primary" :loading="trackingCreating" @click="createTracking">
                创建追踪任务
              </el-button>
            </el-form>
            <p v-if="trackingError" class="error-text">{{ trackingError }}</p>
          </section>

          <div class="section-toolbar">
            <div>
              <h3>追踪任务与历史</h3>
              <p class="muted">手动刷新；自动调度属于后续可选增强。</p>
            </div>
            <el-button :loading="trackingsLoading" @click="loadTrackings">刷新列表</el-button>
          </div>
          <el-empty v-if="!trackingsLoading && !trackings.length" description="还没有追踪任务" />
          <section class="tracking-list">
            <article
              v-for="tracking in trackings"
              :key="tracking.tracking_id"
              class="tracking-card"
            >
              <div class="card-head">
                <div>
                  <h3>{{ tracking.name }}</h3>
                  <p class="muted">
                    {{ tracking.query }} · {{ tracking.field }} · {{ trackingPageSize }} 条/次
                  </p>
                </div>
                <el-button
                  size="small"
                  type="primary"
                  :loading="refreshingTrackingId === tracking.tracking_id"
                  @click="refreshTracking(tracking)"
                >
                  手动刷新
                </el-button>
              </div>
              <div class="tracking-stats">
                <span>最近状态：{{ tracking.latest_run?.status || "尚未运行" }}</span>
                <span>本次新增：{{ tracking.new_count_latest }}</span>
                <span>历史运行：{{ tracking.run_count }}</span>
                <span>
                  最近刷新：{{ tracking.last_refreshed_at ? formatArxivDate(tracking.last_refreshed_at) : "从未" }}
                </span>
              </div>
              <p v-if="tracking.latest_run?.error_message" class="error-text">
                {{ tracking.latest_run.error_message }}
              </p>
              <div v-if="tracking.latest_run?.results?.length" class="tracking-results">
                <article
                  v-for="result in tracking.latest_run.results"
                  :key="`${tracking.tracking_id}-${result.arxiv_id}`"
                  class="mini-result"
                >
                  <div class="tag-row">
                    <el-tag v-if="result.is_new" type="success" size="small">本次新增</el-tag>
                    <el-tag size="small">arXiv:{{ result.arxiv_id }}</el-tag>
                  </div>
                  <strong>{{ result.title }}</strong>
                  <a :href="result.abs_url" target="_blank" rel="noreferrer">查看官方页面</a>
                </article>
              </div>
              <el-table
                v-if="tracking.history.length"
                :data="tracking.history"
                size="small"
                class="history-table"
              >
                <el-table-column prop="run_id" label="运行" width="70" />
                <el-table-column prop="status" label="状态" width="100" />
                <el-table-column prop="total_results" label="返回" width="80" />
                <el-table-column prop="new_result_count" label="新增" width="80" />
                <el-table-column prop="started_at" label="开始时间" min-width="180" />
              </el-table>
            </article>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="相关推荐" name="recommendations" lazy>
        <div class="tab-body">
          <section class="recommendation-head">
            <div>
              <h3>基于收藏与追踪关键词的推荐</h3>
              <p class="muted">
                外部推荐来自 arXiv；本地推荐复用现有 RAG，两者分别标识。
              </p>
            </div>
            <el-button
              type="primary"
              :loading="recommendationLoading"
              @click="loadRecommendations"
            >
              生成推荐
            </el-button>
          </section>
          <p v-if="recommendationError" class="error-text">{{ recommendationError }}</p>
          <template v-if="recommendations">
            <p class="disclaimer">{{ recommendations.disclaimer }}</p>
            <div class="seed-box">
              <strong>推荐种子</strong>
              <span>{{ recommendations.seed.query }}</span>
              <el-tag size="small">
                {{ recommendations.seed.source === "favorite_paper" ? "收藏论文" : "追踪关键词" }}
              </el-tag>
            </div>

            <section>
              <div class="source-title">
                <h3>arXiv 外部推荐</h3>
                <el-tag type="warning">外部结果</el-tag>
              </div>
              <el-empty
                v-if="!recommendations.external_results.length"
                description="arXiv 暂无相关结果"
              />
              <div class="recommendation-grid">
                <article
                  v-for="result in recommendations.external_results"
                  :key="result.arxiv_id"
                  class="recommendation-card"
                >
                  <div class="tag-row">
                    <el-tag size="small" type="warning">arXiv:{{ result.arxiv_id }}</el-tag>
                    <el-tag v-if="result.in_local_library" size="small" type="success">
                      本地已有
                    </el-tag>
                  </div>
                  <h4>{{ result.title }}</h4>
                  <p>{{ result.summary }}</p>
                  <a :href="result.abs_url" target="_blank" rel="noreferrer">
                    打开官方页面
                  </a>
                </article>
              </div>
            </section>

            <section>
              <div class="source-title">
                <h3>本地 RAG 推荐</h3>
                <el-tag type="success">本地论文</el-tag>
              </div>
              <el-alert
                v-if="recommendations.local_rag_status === 'unavailable'"
                type="warning"
                :closable="false"
                title="本地 RAG 暂时不可用，本次仅展示真实外部结果"
              />
              <el-empty
                v-else-if="!recommendations.local_recommendations.length"
                description="本地论文库暂无相关证据"
              />
              <div class="recommendation-grid">
                <article
                  v-for="paper in recommendations.local_recommendations"
                  :key="paper.paper_id"
                  class="recommendation-card local"
                >
                  <div class="tag-row">
                    <el-tag size="small" type="success">本地已有论文</el-tag>
                    <el-tag size="small">Page {{ paper.page_number }}</el-tag>
                  </div>
                  <h4>{{ paper.title }}</h4>
                  <p>{{ paper.evidence }}</p>
                  <el-button link type="primary" @click="openLocalPaper(paper.paper_id, paper.page_number)">
                    打开本地证据
                  </el-button>
                </article>
              </div>
            </section>
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  createDiscoveryTracking,
  favoriteExternal,
  fetchDiscoveryRecommendations,
  fetchDiscoveryTrackings,
  fetchExternalFavorites,
  importFavoritePdf,
  patchFavoriteState,
  refreshDiscoveryTracking,
  searchExternalLiterature,
} from "@/api/discovery";
import { ApiError } from "@/api/http";
import type {
  DiscoveryField,
  DiscoveryRecommendations,
  DiscoverySortBy,
  DiscoverySortOrder,
  ExternalFavorite,
  ExternalPaperResult,
  ExternalSearchResult,
  TrackingSearch,
} from "@/types/discovery";
import {
  buildTrackingPayload,
  externalResultLabel,
  favoriteImportStatusLabel,
  favoriteImportStatusType,
  formatArxivDate,
} from "@/utils/discovery";

const router = useRouter();
const activeTab = ref("search");
const searchQuery = ref("");
const searchField = ref<DiscoveryField>("all");
const searchSort = ref<DiscoverySortBy>("relevance");
const searchOrder = ref<DiscoverySortOrder>("descending");
const searchPageSize = ref(10);
const searchResult = ref<ExternalSearchResult | null>(null);
const searchLoading = ref(false);
const searchError = ref("");
const favoritingId = ref("");

const favorites = ref<ExternalFavorite[]>([]);
const favoritesLoading = ref(false);
const favoritesError = ref("");
const importingFavoriteId = ref<number | null>(null);

const trackingName = ref("");
const trackingQuery = ref("");
const trackingField = ref<DiscoveryField>("all");
const trackingPageSize = ref(20);
const trackings = ref<TrackingSearch[]>([]);
const trackingsLoading = ref(false);
const trackingCreating = ref(false);
const trackingError = ref("");
const refreshingTrackingId = ref<number | null>(null);

const recommendations = ref<DiscoveryRecommendations | null>(null);
const recommendationLoading = ref(false);
const recommendationError = ref("");

async function runSearch(page: number) {
  const query = searchQuery.value.trim();
  if (query.length < 2) return;
  searchLoading.value = true;
  searchError.value = "";
  try {
    searchResult.value = await searchExternalLiterature({
      query,
      field: searchField.value,
      page,
      pageSize: searchPageSize.value,
      sortBy: searchSort.value,
      sortOrder: searchOrder.value,
    });
  } catch (error) {
    searchError.value = error instanceof ApiError ? error.message : "arXiv 搜索失败";
  } finally {
    searchLoading.value = false;
  }
}

async function favoritePaper(paper: ExternalPaperResult) {
  favoritingId.value = paper.arxiv_id;
  try {
    const favorite = await favoriteExternal(paper.arxiv_id);
    if (searchResult.value) {
      searchResult.value.results = searchResult.value.results.map((item) =>
        item.arxiv_id === paper.arxiv_id
          ? patchFavoriteState(item, favorite)
          : item,
      );
    }
    ElMessage.success("已收藏；原始 arXiv 元数据已由服务端复核");
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "收藏失败");
  } finally {
    favoritingId.value = "";
  }
}

async function loadFavorites() {
  favoritesLoading.value = true;
  favoritesError.value = "";
  try {
    favorites.value = await fetchExternalFavorites();
  } catch (error) {
    favoritesError.value = error instanceof ApiError ? error.message : "加载收藏失败";
  } finally {
    favoritesLoading.value = false;
  }
}

async function importFavorite(favorite: ExternalFavorite) {
  if (!favorite.favorite_id) return;
  importingFavoriteId.value = favorite.favorite_id;
  try {
    const result = await importFavoritePdf(favorite.favorite_id);
    favorite.import_status = result.favorite.import_status;
    favorite.import_job = result.import_job;
    favorite.local_paper_id = result.favorite.local_paper_id;
    favorite.import_error = result.favorite.import_error;
    ElMessage.success(
      result.status === "already_in_library"
        ? "检测到本地已有相同 arXiv 论文或 PDF SHA"
        : "PDF 已进入现有导入任务队列",
    );
    await loadFavorites();
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "PDF 入库失败");
    await loadFavorites();
  } finally {
    importingFavoriteId.value = null;
  }
}

async function createTracking() {
  trackingCreating.value = true;
  trackingError.value = "";
  try {
    const payload = buildTrackingPayload({
      name: trackingName.value,
      query: trackingQuery.value,
      field: trackingField.value,
      sortBy: "submitted",
      sortOrder: "descending",
      pageSize: trackingPageSize.value,
    });
    await createDiscoveryTracking(payload);
    trackingName.value = "";
    trackingQuery.value = "";
    await loadTrackings();
    ElMessage.success("追踪任务已保存");
  } catch (error) {
    trackingError.value = error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : "创建追踪失败";
  } finally {
    trackingCreating.value = false;
  }
}

async function loadTrackings() {
  trackingsLoading.value = true;
  try {
    trackings.value = await fetchDiscoveryTrackings();
  } catch (error) {
    trackingError.value = error instanceof ApiError ? error.message : "加载追踪失败";
  } finally {
    trackingsLoading.value = false;
  }
}

async function refreshTracking(tracking: TrackingSearch) {
  refreshingTrackingId.value = tracking.tracking_id;
  try {
    const updated = await refreshDiscoveryTracking(tracking.tracking_id);
    trackings.value = trackings.value.map((item) =>
      item.tracking_id === updated.tracking_id ? updated : item,
    );
    ElMessage.success(`刷新完成，本次新增 ${updated.new_count_latest} 篇`);
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "追踪刷新失败");
    await loadTrackings();
  } finally {
    refreshingTrackingId.value = null;
  }
}

async function loadRecommendations() {
  recommendationLoading.value = true;
  recommendationError.value = "";
  try {
    recommendations.value = await fetchDiscoveryRecommendations();
  } catch (error) {
    recommendationError.value = error instanceof ApiError
      ? error.message
      : "推荐生成失败";
  } finally {
    recommendationLoading.value = false;
  }
}

function openLocalPaper(paperId: number, page = 1) {
  router.push({
    name: "paper-detail",
    params: { id: paperId },
    query: { page: String(page) },
  });
}

watch(activeTab, (tab) => {
  if (tab === "favorites") void loadFavorites();
  if (tab === "tracking") void loadTrackings();
});

onMounted(() => {
  void loadTrackings();
});
</script>

<style scoped>
.discovery-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1380px;
  margin: 0 auto;
}

.discovery-hero,
.card-head,
.search-actions,
.result-toolbar,
.section-toolbar,
.recommendation-head,
.source-title,
.seed-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.discovery-hero {
  padding: 22px 24px;
}

.discovery-hero h2 {
  margin: 4px 0 6px;
}

.eyebrow {
  margin: 0;
  color: var(--accent-2);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.hero-tags,
.tag-row,
.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.discovery-tabs {
  padding: 8px 18px 22px;
}

.tab-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.search-panel,
.tracking-form {
  padding: 18px;
  background: #f7fafb;
  border: 1px solid var(--line);
  border-radius: 14px;
}

.search-row {
  display: grid;
  grid-template-columns: minmax(280px, 2fr) repeat(3, minmax(130px, 1fr));
  gap: 12px;
}

.query-field {
  min-width: 240px;
}

.external-list,
.tracking-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.external-card,
.tracking-card,
.recommendation-card {
  padding: 18px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: 0 8px 20px rgba(15, 42, 61, 0.05);
}

.external-card h3,
.tracking-card h3 {
  margin: 10px 0 0;
}

.date {
  white-space: nowrap;
  font-size: 12px;
}

.authors {
  color: #466476;
  font-size: 13px;
}

.summary {
  color: #536c7c;
  line-height: 1.7;
}

.error-text {
  color: var(--danger);
}

.job-progress {
  max-width: 420px;
  margin: 10px 0;
}

.job-progress span {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 12px;
}

.tracking-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  padding: 10px 0;
  color: var(--muted);
  font-size: 13px;
}

.tracking-results {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 10px 0;
}

.mini-result {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: #f7fafb;
  border-radius: 9px;
}

.mini-result a,
.recommendation-card a {
  color: var(--accent);
  font-size: 12px;
}

.history-table {
  margin-top: 10px;
}

.seed-box,
.disclaimer {
  padding: 12px 14px;
  border-radius: 10px;
  background: #fff8e8;
  color: #72511d;
}

.seed-box {
  justify-content: flex-start;
}

.recommendation-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.recommendation-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 3px solid var(--accent-2);
}

.recommendation-card.local {
  border-top-color: var(--ok);
}

.recommendation-card h4 {
  margin: 0;
}

.recommendation-card p {
  color: var(--muted);
  line-height: 1.65;
}

@media (max-width: 1050px) {
  .search-row,
  .recommendation-grid,
  .tracking-results {
    grid-template-columns: 1fr;
  }
}
</style>
