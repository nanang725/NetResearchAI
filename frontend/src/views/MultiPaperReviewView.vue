<template>
  <div class="review-page">
    <section class="page-card review-workspace">
      <div class="workspace-heading">
        <div>
          <p class="eyebrow">MULTI-PAPER EVIDENCE SYNTHESIS</p>
          <h2>多文献证据综述</h2>
          <p class="muted">
            选择 2～5 篇本地论文，系统逐篇检索证据并比较方法、结果、结论与局限。
          </p>
        </div>
        <div class="workspace-actions">
          <el-tag v-if="reviewSession" effect="plain">
            {{ reviewSession.status === "completed" ? "已保存综述" : "任务已恢复" }}
          </el-tag>
          <el-tag effect="plain">证据约束生成</el-tag>
          <el-button
            v-if="reviewSession || review || selectedPaperIds.length || topic"
            plain
            @click="startNewReview"
          >
            重新开始
          </el-button>
        </div>
      </div>

      <div class="review-form-grid">
        <el-form label-position="top" class="review-form">
          <el-form-item label="选择本地论文">
            <el-select
              v-model="selectedPaperIds"
              multiple
              filterable
              :multiple-limit="5"
              placeholder="至少选择 2 篇论文"
              style="width: 100%"
            >
              <el-option
                v-for="paper in papers"
                :key="paper.id"
                :label="paperLabel(paper)"
                :value="paper.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="研究问题 / 综述主题">
            <el-input
              v-model="topic"
              type="textarea"
              :rows="3"
              maxlength="200"
              show-word-limit
              placeholder="例如：大语言模型检索增强生成的关键方法、证据与局限"
            />
          </el-form-item>
          <el-form-item>
            <el-checkbox v-model="includeFutureWork">
              同时提取 Future Work / Future Directions
            </el-checkbox>
          </el-form-item>
          <div class="form-actions">
            <div>
              <strong>{{ selectedPaperIds.length }} / 5</strong>
              <span>已选择论文</span>
            </div>
            <el-button
              type="primary"
              :loading="loading"
              :disabled="selectionError !== null || topic.trim().length < 3"
              @click="generateReview"
            >
              生成证据综述
            </el-button>
          </div>
          <p v-if="selectionError" class="form-error">{{ selectionError }}</p>
          <p v-if="loading" class="generation-status" aria-live="polite">
            正在逐篇检索证据并比较，已等待 {{ loadingElapsedText }}。该过程依赖真实论文与模型响应。
          </p>
        </el-form>

        <aside class="selected-papers">
          <div class="selected-heading">
            <strong>已选文献</strong>
            <span>{{ selectedPaperItems.length }} 篇</span>
          </div>
          <el-empty
            v-if="!selectedPaperItems.length"
            description="从左侧选择至少 2 篇论文"
            :image-size="58"
          />
          <article v-for="paper in selectedPaperItems" :key="paper.id">
            <div>
              <strong>{{ paper.title }}</strong>
              <span>
                {{ paper.publication_year || "年份未知" }} ·
                {{ statusLabel(paper.index_status) }}
              </span>
            </div>
            <button
              type="button"
              aria-label="移除论文"
              @click="removePaper(paper.id)"
            >
              ×
            </button>
          </article>
          <p class="selection-note">
            只允许选择当前本地论文。证据不足的维度会在生成结果中明确标注。
          </p>
        </aside>
      </div>
    </section>

    <div v-if="errorMessage" class="page-card error-box">
      <span>{{ errorMessage }}</span>
      <el-button link type="primary" @click="retryReview">重试</el-button>
    </div>

    <section v-if="loading" class="page-card generation-loading" aria-live="polite">
      <div>
        <p class="eyebrow">RESEARCH IN PROGRESS</p>
        <h3>正在构建多文献证据视图</h3>
        <p class="muted">
          当前已等待 {{ loadingElapsedText }}。单篇证据不足不会用推断或模拟内容补足。
        </p>
      </div>
      <el-skeleton :rows="5" animated />
    </section>

    <template v-if="review">
      <section class="page-card review-cover">
        <div class="cover-heading">
          <el-tag type="warning" effect="plain">AI 辅助生成</el-tag>
          <h2>{{ review.title }}</h2>
          <p class="muted">主题：{{ review.topic }} · 模型：{{ review.model }}</p>
        </div>
        <div class="cover-metrics">
          <div><strong>{{ review.paper_coverage.length }}</strong><span>参与论文</span></div>
          <div><strong>{{ totalEvidenceCount }}</strong><span>覆盖证据</span></div>
          <div><strong>{{ coveredPaperCount }}</strong><span>已覆盖论文</span></div>
        </div>
        <p class="disclaimer">{{ review.disclaimer }}</p>
      </section>

      <section class="page-card coverage">
        <h3>证据覆盖</h3>
        <el-table :data="review.paper_coverage" size="small">
          <el-table-column prop="title" label="论文" min-width="260" />
          <el-table-column prop="evidence_count" label="主题证据数" width="120" />
          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <el-tag :type="row.status === 'covered' ? 'success' : 'warning'">
                {{ row.status === "covered" ? "已覆盖" : "证据不足" }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="page-card comparison">
        <div class="comparison-head">
          <div>
            <h3>四维证据比较</h3>
            <p class="muted">
              每项均绑定论文、页码和 Chunk；推断与证据不足会明确标注。
            </p>
          </div>
        </div>
        <div class="comparison-grid">
          <article
            v-for="dimension in review.dimension_comparison"
            :key="dimension.dimension"
            class="comparison-card"
          >
            <h4>{{ dimension.label }}</h4>
            <div
              v-for="item in dimension.items"
              :key="`${dimension.dimension}-${item.paper_id}`"
              class="comparison-item"
            >
              <div class="comparison-item-head">
                <strong>{{ item.title }}</strong>
                <el-tag
                  size="small"
                  :type="claimKindType(item.claim_kind)"
                >
                  {{ claimKindLabel(item.claim_kind) }}
                </el-tag>
              </div>
              <p>{{ item.summary }}</p>
              <div v-if="item.citations.length" class="citation-list">
                <el-button
                  v-for="(citation, citationIndex) in item.citations"
                  :key="`${citation.chunk_id}-${citationIndex}`"
                  size="small"
                  plain
                  @click="openCitation(citation)"
                >
                  [{{ citationIndex + 1 }}] 第 {{ citation.page_number }} 页 ·
                  Chunk {{ citation.chunk_id }}
                </el-button>
              </div>
            </div>
          </article>
        </div>
      </section>

      <section
        v-for="(section, sectionIndex) in review.sections"
        :key="`${section.heading}-${sectionIndex}`"
        class="page-card review-section"
      >
        <h3>{{ section.heading }}</h3>
        <p class="review-content">{{ section.content }}</p>
        <div class="citation-list">
          <el-button
            v-for="(citation, citationIndex) in section.citations"
            :key="`${citation.chunk_id}-${citationIndex}`"
            size="small"
            plain
            @click="openCitation(citation)"
          >
            [{{ citationIndex + 1 }}] {{ citation.title }} · 第
            {{ citation.page_number }} 页
          </el-button>
        </div>
        <details
          v-for="(citation, citationIndex) in section.citations"
          :key="`evidence-${citation.chunk_id}-${citationIndex}`"
          class="evidence-details"
        >
          <summary>证据 {{ citationIndex + 1 }} 原文</summary>
          <p>{{ citation.evidence }}</p>
          <el-button link type="primary" @click="openCitation(citation)">
            打开论文第 {{ citation.page_number }} 页
          </el-button>
        </details>
      </section>

      <section v-if="review.future_work.length" class="page-card">
        <h3>Future Work / Future Directions</h3>
        <article
          v-for="(item, index) in review.future_work"
          :key="`${item.chunk_id}-${index}`"
          class="future-item"
        >
          <div class="future-head">
            <el-tag :type="item.kind === 'explicit' ? 'success' : 'warning'">
              {{ item.kind === "explicit" ? "作者明确提出" : "基于局限推断" }}
            </el-tag>
            <strong>{{ item.title }} · 第 {{ item.page_number }} 页</strong>
          </div>
          <p>{{ item.summary }}</p>
          <blockquote>{{ item.original_evidence }}</blockquote>
          <el-button link type="primary" @click="openFutureWork(item)">
            查看原文位置
          </el-button>
        </article>
      </section>

      <section v-if="!review.future_work.length" class="page-card empty-future">
        未从当前证据中找到明确的 Future Work / Future Directions。
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { fetchPapers } from "@/api/papers";
import {
  createLiteratureReviewSession,
  fetchLatestLiteratureReviewSession,
  fetchLiteratureReviewSession,
  retryLiteratureReviewSession,
} from "@/api/citations";
import { ApiError } from "@/api/http";
import type { PaperListItem } from "@/types/paper";
import type {
  LiteratureReview,
  LiteratureReviewSession,
  ReviewEvidenceCitation,
  ReviewFutureWorkItem,
} from "@/types/citation";
import {
  buildReviewRequest,
  paperPageRoute,
  validateReviewPaperSelection,
} from "@/utils/citationGraph";
import {
  claimKindLabel,
  claimKindType,
} from "@/utils/multiPaperReview";
import { statusLabel } from "@/utils/format";

const router = useRouter();
const route = useRoute();
const papers = ref<PaperListItem[]>([]);
const selectedPaperIds = ref<number[]>([]);
const topic = ref("");
const includeFutureWork = ref(true);
const review = ref<LiteratureReview | null>(null);
const reviewSession = ref<LiteratureReviewSession | null>(null);
const loading = ref(false);
const errorMessage = ref("");
const loadingElapsedMs = ref(0);
let loadingTimer: ReturnType<typeof setInterval> | null = null;
let sessionPollTimer: ReturnType<typeof setTimeout> | null = null;

const selectionError = computed(() =>
  validateReviewPaperSelection(selectedPaperIds.value),
);
const selectedPaperItems = computed(() =>
  selectedPaperIds.value
    .map((id) => papers.value.find((paper) => paper.id === id))
    .filter((paper): paper is PaperListItem => Boolean(paper)),
);
const totalEvidenceCount = computed(() =>
  review.value?.paper_coverage.reduce(
    (total, item) => total + item.evidence_count,
    0,
  ) ?? 0,
);
const coveredPaperCount = computed(
  () =>
    review.value?.paper_coverage.filter((item) => item.status === "covered").length
    ?? 0,
);
const loadingElapsedText = computed(
  () => `${(loadingElapsedMs.value / 1000).toFixed(1)} s`,
);

function paperLabel(paper: PaperListItem): string {
  return `${paper.title}${paper.publication_year ? ` (${paper.publication_year})` : ""}`;
}

function removePaper(paperId: number) {
  selectedPaperIds.value = selectedPaperIds.value.filter((id) => id !== paperId);
}

function startLoadingClock() {
  const startedAt = performance.now();
  loadingElapsedMs.value = 0;
  if (loadingTimer) clearInterval(loadingTimer);
  loadingTimer = setInterval(() => {
    loadingElapsedMs.value = performance.now() - startedAt;
  }, 100);
}

function stopLoadingClock() {
  if (loadingTimer) clearInterval(loadingTimer);
  loadingTimer = null;
}

function stopSessionPolling() {
  if (sessionPollTimer) clearTimeout(sessionPollTimer);
  sessionPollTimer = null;
}

function syncSessionRoute(reviewId: string) {
  if (route.query.review === reviewId) return;
  void router.replace({
    name: "review",
    query: { ...route.query, review: reviewId },
  });
}

function applySession(session: LiteratureReviewSession) {
  reviewSession.value = session;
  selectedPaperIds.value = [...session.selected_paper_ids];
  topic.value = session.topic;
  includeFutureWork.value = session.include_future_work;
  review.value = session.result;
  errorMessage.value = session.status === "failed"
    ? session.error || "综述生成失败，请稍后重试。"
    : "";
  if (session.status === "queued" || session.status === "running") {
    loading.value = true;
    startLoadingClock();
  } else {
    loading.value = false;
    stopLoadingClock();
  }
}

async function pollSession(reviewId: string) {
  stopSessionPolling();
  try {
    const session = await fetchLiteratureReviewSession(reviewId);
    applySession(session);
    if (session.status === "queued" || session.status === "running") {
      sessionPollTimer = setTimeout(() => void pollSession(reviewId), 1500);
    }
  } catch (error) {
    errorMessage.value = error instanceof ApiError
      ? error.message
      : "综述任务状态读取失败。";
    sessionPollTimer = setTimeout(() => void pollSession(reviewId), 2500);
  }
}

async function restoreReviewSession(reviewId?: string) {
  try {
    const session = reviewId
      ? await fetchLiteratureReviewSession(reviewId)
      : await fetchLatestLiteratureReviewSession();
    if (!session) return;
    applySession(session);
    syncSessionRoute(session.review_id);
    if (session.status === "queued" || session.status === "running") {
      await pollSession(session.review_id);
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && reviewId) {
      errorMessage.value = "上次综述任务已不存在，可重新开始。";
      return;
    }
    ElMessage.error(
      error instanceof ApiError ? error.message : "恢复综述任务失败。",
    );
  }
}

async function generateReview() {
  const validation = validateReviewPaperSelection(selectedPaperIds.value);
  if (validation) {
    ElMessage.warning(validation);
    return;
  }
  if (topic.value.trim().length < 3) {
    ElMessage.warning("请输入至少 3 个字符的综述主题。");
    return;
  }
  loading.value = true;
  errorMessage.value = "";
  startLoadingClock();
  try {
    const session = await createLiteratureReviewSession(
      buildReviewRequest(
        selectedPaperIds.value,
        topic.value,
        includeFutureWork.value,
      ),
    );
    applySession(session);
    syncSessionRoute(session.review_id);
    await pollSession(session.review_id);
  } catch (error) {
    errorMessage.value = error instanceof ApiError
      ? error.message
      : "生成综述失败，请稍后重试。";
  } finally {
    if (reviewSession.value?.status !== "queued"
      && reviewSession.value?.status !== "running") {
      stopLoadingClock();
      loading.value = false;
    }
  }
}

function retryReview() {
  if (reviewSession.value?.status === "failed") {
    loading.value = true;
    errorMessage.value = "";
    void retryLiteratureReviewSession(reviewSession.value.review_id)
      .then((session) => {
        applySession(session);
        return pollSession(session.review_id);
      })
      .catch((error) => {
        loading.value = false;
        errorMessage.value = error instanceof ApiError
          ? error.message
          : "综述重试失败。";
      });
    return;
  }
  void generateReview();
}

function startNewReview() {
  stopSessionPolling();
  stopLoadingClock();
  reviewSession.value = null;
  review.value = null;
  selectedPaperIds.value = [];
  topic.value = "";
  includeFutureWork.value = true;
  loading.value = false;
  errorMessage.value = "";
  void router.replace({ name: "review" });
}

function openCitation(citation: ReviewEvidenceCitation) {
  router.push(paperPageRoute(citation.paper_id, citation.page_number));
}

function openFutureWork(item: ReviewFutureWorkItem) {
  router.push(paperPageRoute(item.paper_id, item.page_number));
}

onMounted(async () => {
  try {
    papers.value = await fetchPapers();
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "加载论文列表失败");
  }
  const requestedReviewId = typeof route.query.review === "string"
    ? route.query.review
    : undefined;
  await restoreReviewSession(requestedReviewId);
});

onUnmounted(() => {
  stopLoadingClock();
  stopSessionPolling();
});
</script>

<style scoped>
.review-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1180px;
  margin: 0 auto;
}

.review-workspace {
  padding: 20px;
}

.review-cover,
.coverage,
.comparison,
.review-section,
.empty-future {
  padding: 20px;
}

.review-form h2,
.review-section h3 {
  margin: 0 0 8px;
}

.workspace-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.workspace-heading {
  margin-bottom: 18px;
}

.workspace-heading > div:first-child {
  min-width: 0;
}

.workspace-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}

.workspace-heading h2 {
  margin: 4px 0 5px;
  font-size: 22px;
  letter-spacing: -0.025em;
}

.workspace-heading p {
  margin: 0;
}

.review-form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 18px;
}

.review-form {
  min-width: 0;
}

.selected-papers {
  padding: 14px;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-lg);
}

.selected-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 9px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.selected-heading span {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.selected-papers article {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 0;
  border-bottom: 1px solid var(--color-border-subtle);
}

.selected-papers article > div {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}

.selected-papers article strong {
  display: -webkit-box;
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: 12px;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.selected-papers article span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.selected-papers article button {
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  font-size: 16px;
}

.selected-papers article button:hover {
  color: var(--color-danger);
  background: var(--color-danger-soft);
}

.selection-note {
  margin: 10px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.55;
}

.form-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.form-actions > div {
  display: flex;
  flex-direction: column;
}

.form-actions strong {
  font-size: 18px;
}

.form-actions span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.form-error,
.error-box {
  color: var(--color-danger);
}

.error-box {
  display: flex;
  justify-content: space-between;
  background: var(--color-danger-soft);
}

.generation-status {
  padding: 10px 12px;
  margin: 10px 0 0;
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border-radius: var(--radius-md);
  font-size: 12px;
}

.generation-loading {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 24px;
  padding: 20px;
}

.generation-loading h3 {
  margin: 4px 0;
}

.generation-loading p {
  margin: 0;
}

.review-cover h2 {
  margin: 12px 0 6px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: normal;
}

.review-cover {
  display: grid;
  min-width: 0;
  align-items: start;
  grid-template-columns: minmax(0, 1fr) minmax(280px, 380px);
  gap: 18px 24px;
}

.cover-heading {
  min-width: 0;
}

.cover-metrics {
  display: grid;
  min-width: 0;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.cover-metrics div {
  display: flex;
  min-width: 0;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--color-surface) 72%, transparent);
  border-radius: var(--radius-md);
  flex-direction: column;
}

.cover-metrics strong {
  font-size: 20px;
}

.cover-metrics span {
  color: var(--color-text-tertiary);
  font-size: 10px;
  white-space: nowrap;
}

.disclaimer {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border-radius: 8px;
  padding: 10px 12px;
}

.review-content {
  white-space: pre-wrap;
  line-height: 1.8;
}

.citation-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.evidence-details {
  margin-top: 10px;
  padding: 10px 12px;
  background: var(--color-bg-subtle);
  border-radius: 8px;
}

.evidence-details p,
.future-item blockquote {
  white-space: pre-wrap;
  color: var(--color-text-secondary);
}

.comparison-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.comparison-card {
  min-width: 0;
  padding: 14px;
  background: var(--color-bg-subtle);
  border-radius: 10px;
}

.comparison-card h4 {
  margin: 0 0 10px;
}

.comparison-item {
  padding: 10px 0;
  border-top: 1px solid var(--line);
}

.comparison-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.comparison-item-head strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

.comparison-item-head :deep(.el-tag) {
  flex: 0 0 auto;
}

.comparison-item p {
  line-height: 1.65;
  color: var(--color-text-secondary);
}

@media (max-width: 900px) {
  .review-form-grid,
  .generation-loading {
    grid-template-columns: 1fr;
  }

  .comparison-grid {
    grid-template-columns: 1fr;
  }

  .workspace-heading {
    flex-direction: column;
  }

  .workspace-actions {
    justify-content: flex-start;
  }

  .review-cover {
    grid-template-columns: 1fr;
  }

  .disclaimer {
    grid-column: auto;
  }
}

@media (max-width: 620px) {
  .review-workspace,
  .review-cover,
  .coverage,
  .comparison,
  .review-section,
  .empty-future {
    padding: 15px;
  }

  .cover-metrics {
    width: 100%;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .form-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .form-actions :deep(.el-button) {
    width: 100%;
  }
}

@media (max-width: 420px) {
  .cover-metrics {
    grid-template-columns: 1fr;
  }

  .cover-metrics span {
    white-space: normal;
  }
}

.future-item + .future-item {
  border-top: 1px solid var(--line);
  margin-top: 16px;
  padding-top: 16px;
}

.future-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
</style>
