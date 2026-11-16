<template>
  <div class="review-page">
    <section class="page-card review-form">
      <div>
        <h2>多文献证据综述</h2>
        <p class="muted">
          选择 2～5 篇本地论文，系统会逐篇检索证据并生成可回溯综述。
        </p>
      </div>

      <el-form label-position="top">
        <el-form-item label="选择论文">
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
        <el-form-item label="综述主题">
          <el-input
            v-model="topic"
            maxlength="200"
            show-word-limit
            placeholder="例如：大语言模型检索增强生成的关键方法与局限"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="includeFutureWork">
            提取 Future Work / Future Directions
          </el-checkbox>
        </el-form-item>
        <div class="form-actions">
          <span class="muted">已选择 {{ selectedPaperIds.length }} / 5 篇</span>
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
      </el-form>
    </section>

    <div v-if="errorMessage" class="page-card error-box">
      <span>{{ errorMessage }}</span>
      <el-button link type="primary" @click="retryReview">重试</el-button>
    </div>

    <template v-if="review">
      <section class="page-card review-cover">
        <el-tag type="warning">AI 辅助生成</el-tag>
        <h2>{{ review.title }}</h2>
        <p class="muted">主题：{{ review.topic }} · 模型：{{ review.model }}</p>
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
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { fetchPapers } from "@/api/papers";
import { generateLiteratureReview } from "@/api/citations";
import { ApiError } from "@/api/http";
import type { PaperListItem } from "@/types/paper";
import type {
  LiteratureReview,
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

const router = useRouter();
const papers = ref<PaperListItem[]>([]);
const selectedPaperIds = ref<number[]>([]);
const topic = ref("");
const includeFutureWork = ref(true);
const review = ref<LiteratureReview | null>(null);
const loading = ref(false);
const errorMessage = ref("");

const selectionError = computed(() =>
  validateReviewPaperSelection(selectedPaperIds.value),
);

function paperLabel(paper: PaperListItem): string {
  return `${paper.title}${paper.publication_year ? ` (${paper.publication_year})` : ""}`;
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
  try {
    review.value = await generateLiteratureReview(
      buildReviewRequest(
        selectedPaperIds.value,
        topic.value,
        includeFutureWork.value,
      ),
    );
  } catch (error) {
    errorMessage.value = error instanceof ApiError
      ? error.message
      : "生成综述失败，请稍后重试。";
  } finally {
    loading.value = false;
  }
}

function retryReview() {
  void generateReview();
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

.review-form,
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

.form-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.form-error,
.error-box {
  color: #b42318;
}

.error-box {
  display: flex;
  justify-content: space-between;
  background: #fff1f0;
}

.review-cover h2 {
  margin: 12px 0 6px;
}

.disclaimer {
  color: #8a5b16;
  background: #fff7e8;
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
  background: #f7fafc;
  border-radius: 8px;
}

.evidence-details p,
.future-item blockquote {
  white-space: pre-wrap;
  color: #566573;
}

.comparison-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.comparison-card {
  padding: 14px;
  background: #f7fafc;
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

.comparison-item p {
  line-height: 1.65;
  color: #536c7c;
}

@media (max-width: 900px) {
  .comparison-grid {
    grid-template-columns: 1fr;
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
