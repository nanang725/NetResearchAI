<template>
  <div class="writing-page">
    <section class="page-card writing-hero">
      <div>
        <p class="eyebrow">MODULE 6 · EVIDENCE-GROUNDED WORKSPACE</p>
        <h2>学术写作与数据可视化</h2>
        <p class="muted">
          大纲和综述回链本地论文证据，References 只读取可核验馆藏，
          CSV 图表只使用本次上传的真实数据。
        </p>
      </div>
      <div class="integrity-badges">
        <el-tag type="success">本地证据</el-tag>
        <el-tag type="warning">未知字段不推断</el-tag>
        <el-tag>真实 CSV 统计</el-tag>
      </div>
    </section>

    <el-tabs v-model="activeTab" class="page-card writing-tabs">
      <el-tab-pane label="论文大纲" name="outline">
        <div class="tab-body">
          <section class="form-panel">
            <h3>生成证据约束大纲</h3>
            <el-form label-position="top">
              <el-form-item label="本地论文">
                <el-select
                  v-model="outlinePaperId"
                  filterable
                  placeholder="选择一篇已解析论文"
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
              <el-form-item label="写作主题">
                <el-input
                  v-model="outlineTopic"
                  maxlength="200"
                  show-word-limit
                  placeholder="例如：大语言模型检索增强方法的研究问题与证据"
                />
              </el-form-item>
              <el-button
                type="primary"
                :loading="outlineLoading"
                :disabled="!outlinePaperId"
                @click="createOutline"
              >
                基于论文内容生成
              </el-button>
            </el-form>
            <p v-if="outlineError" class="error-text">{{ outlineError }}</p>
            <p v-else-if="outlineElapsedMs !== null" class="request-meta">
              最近一次大纲生成实测 {{ outlineElapsedMs }} ms。
            </p>
          </section>

          <section v-if="outlineLoading" class="page-card loading-panel" aria-live="polite">
            <div>
              <h3>正在生成证据约束大纲</h3>
              <p class="muted">系统会检索真实论文 Chunk，证据不足的章节不会被填充。</p>
            </div>
            <el-skeleton :rows="6" animated />
          </section>

          <template v-if="outline">
            <section class="result-cover">
              <div>
                <el-tag type="success">系统按证据组织</el-tag>
                <h2>{{ outline.title }}</h2>
                <p class="muted">
                  {{ outline.paper.authors || "作者未知" }} ·
                  {{ outline.paper.publication_year || "年份未知" }} ·
                  {{ outline.evidence_chunk_count }} 个证据 Chunk
                </p>
              </div>
              <div class="translation-controls">
                <el-select v-model="translationDirection" size="small">
                  <el-option label="中译英" value="zh_to_en" />
                  <el-option label="英译中" value="en_to_zh" />
                </el-select>
                <el-button
                  size="small"
                  :loading="translationLoading"
                  @click="translateOutline"
                >
                  翻译大纲
                </el-button>
              </div>
              <p class="disclaimer">{{ outline.disclaimer }}</p>
            </section>

            <section class="content-labels">
              <article>
                <strong>原始证据</strong>
                <span>{{ outline.content_labels.original_evidence }}</span>
              </article>
              <article>
                <strong>系统生成内容</strong>
                <span>{{ outline.content_labels.generated_content }}</span>
              </article>
              <article>
                <strong>统计结果</strong>
                <span>{{ outline.content_labels.statistics }}</span>
              </article>
            </section>

            <section class="outline-list">
              <article
                v-for="(section, index) in outline.sections"
                :key="`${section.heading}-${index}`"
                class="outline-section"
                :class="{ child: section.level > 1, unsupported: section.evidence_status === 'insufficient_evidence' }"
              >
                <div class="section-head">
                  <h3>{{ section.heading }}</h3>
                  <el-tag
                    :type="section.evidence_status === 'covered' ? 'success' : 'warning'"
                    size="small"
                  >
                    {{ section.evidence_status === "covered" ? "证据已覆盖" : "证据不足" }}
                  </el-tag>
                </div>
                <ul>
                  <li v-for="point in section.writing_points" :key="point">
                    {{ point }}
                  </li>
                </ul>
                <p v-if="section.unsupported_note" class="unsupported-note">
                  {{ section.unsupported_note }}
                </p>
                <div v-if="section.evidence.length" class="evidence-grid">
                  <article
                    v-for="evidence in section.evidence"
                    :key="`${section.heading}-${evidence.chunk_id}`"
                    class="evidence-card"
                  >
                    <div>
                      <strong>证据 {{ evidence.citation_id }}</strong>
                      <el-tag size="small" type="info">
                        第 {{ evidence.page_number }} 页 · Chunk {{ evidence.chunk_id }}
                      </el-tag>
                    </div>
                    <p>{{ evidence.excerpt }}</p>
                    <el-button link type="primary" @click="openEvidence(evidence)">
                      打开原文
                    </el-button>
                  </article>
                </div>
              </article>
            </section>

            <section v-if="translation" class="translation-result">
              <div class="section-head">
                <h3>{{ translation.source_language }} → {{ translation.target_language }} 翻译</h3>
                <el-tag type="warning">翻译辅助内容</el-tag>
              </div>
              <pre>{{ translation.translated_text }}</pre>
              <p class="muted">{{ translation.disclaimer }}</p>
            </section>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="文献综述" name="review" lazy>
        <div class="embedded-review">
          <MultiPaperReviewView />
        </div>
      </el-tab-pane>

      <el-tab-pane label="References" name="references" lazy>
        <div class="tab-body">
          <section class="form-panel reference-form">
            <div>
              <h3>本地可核验 References</h3>
              <p class="muted">
                仅复制 SQLite 中的真实字段；缺失字段显示为未知，并由系统跳过生成。
              </p>
            </div>
            <el-select
              v-model="referencePaperIds"
              multiple
              filterable
              collapse-tags
              placeholder="不选则使用全部本地论文"
              style="width: 100%"
            >
              <el-option
                v-for="paper in papers"
                :key="paper.id"
                :label="paperLabel(paper)"
                :value="paper.id"
              />
            </el-select>
            <el-button
              type="primary"
              :loading="referencesLoading"
              @click="loadReferences"
            >
              核验并生成 References
            </el-button>
            <p v-if="referencesError" class="error-text">{{ referencesError }}</p>
            <p v-else-if="referencesElapsedMs !== null" class="request-meta">
              最近一次核验实测 {{ referencesElapsedMs }} ms。
            </p>
          </section>

          <section v-if="referencesLoading" class="page-card loading-panel">
            <el-skeleton :rows="5" animated />
          </section>

          <template v-if="references">
            <section class="reference-summary">
              <div><strong>{{ references.total_count }}</strong><span>本地条目</span></div>
              <div><strong>{{ references.verified_count }}</strong><span>可核验引用</span></div>
              <div><strong>{{ references.unknown_field_count }}</strong><span>未知字段</span></div>
            </section>
            <p class="disclaimer">{{ references.disclaimer }}</p>
            <el-empty
              v-if="!references.references.length"
              description="本地论文库为空"
            />
            <section class="reference-list">
              <article
                v-for="reference in references.references"
                :key="reference.paper_id"
                class="reference-card"
              >
                <div class="section-head">
                  <h3>{{ reference.title || "标题未知" }}</h3>
                  <el-tag :type="reference.can_cite ? 'success' : 'warning'">
                    {{ reference.can_cite ? "可核验" : "不可引用" }}
                  </el-tag>
                </div>
                <p class="citation-line">{{ reference.citation_text }}</p>
                <div class="unknown-tags">
                  <el-tag
                    v-for="field in reference.unknown_fields"
                    :key="field"
                    size="small"
                    type="warning"
                  >
                    {{ field }}：未知
                  </el-tag>
                </div>
                <blockquote v-if="reference.evidence">
                  {{ reference.evidence.excerpt }}
                </blockquote>
                <p class="muted">{{ reference.verification_note }}</p>
                <details>
                  <summary>查看 BibTeX</summary>
                  <pre>{{ reference.bibtex }}</pre>
                </details>
              </article>
            </section>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="CSV 数据可视化" name="data" lazy>
        <div class="tab-body">
          <section class="form-panel csv-form">
            <div>
              <h3>上传 CSV 并生成真实图表</h3>
              <p class="muted">最大 5MB、20,000 行；所有图表数值来自上传文件。</p>
            </div>
            <input
              ref="csvInput"
              type="file"
              accept=".csv,text/csv"
              class="hidden-input"
              @change="handleCsvFile"
            />
            <button
              type="button"
              class="csv-dropzone"
              :disabled="csvLoading"
              @click="csvInput?.click()"
              @dragover.prevent
              @drop.prevent="handleCsvDrop"
            >
              <UploadFilled />
              <span>
                <strong>{{ csvLoading ? "正在分析 CSV" : "拖入 CSV 或点击选择文件" }}</strong>
                <small>文件只用于当前真实统计与图表，不生成模拟数值</small>
              </span>
              <el-tag v-if="csvFile" effect="plain">{{ formatBytes(csvFile.size) }}</el-tag>
            </button>
            <div v-if="csvFile" class="csv-file-info">
              <div>
                <span>当前文件</span>
                <strong>{{ csvFile.name }}</strong>
              </div>
              <span v-if="csvElapsedMs !== null">
                分析实测 {{ csvElapsedMs }} ms
              </span>
            </div>
            <div v-if="csvError" class="inline-error">
              <span>{{ csvError }}</span>
              <el-button link type="primary" :disabled="!csvFile" @click="retryCsvAnalysis">
                重试
              </el-button>
            </div>
          </section>

          <section v-if="csvLoading" class="page-card loading-panel">
            <h3>正在读取真实数据</h3>
            <el-skeleton :rows="6" animated />
          </section>

          <template v-if="csvAnalysis">
            <section class="csv-stats">
              <div><strong>{{ csvAnalysis.row_count }}</strong><span>数据行</span></div>
              <div><strong>{{ csvAnalysis.column_count }}</strong><span>字段</span></div>
              <div><strong>{{ csvAnalysis.missing_cell_count }}</strong><span>缺失单元格</span></div>
              <div><strong>{{ csvAnalysis.chart.categories.length }}</strong><span>图表类别</span></div>
            </section>

            <section class="data-labels">
              <article>
                <strong>原始证据</strong>
                <span>{{ csvAnalysis.content_labels.original_evidence }}</span>
              </article>
              <article>
                <strong>系统生成内容</strong>
                <span>{{ csvAnalysis.content_labels.generated_content }}</span>
              </article>
              <article>
                <strong>统计结果</strong>
                <span>{{ csvAnalysis.content_labels.statistics }}</span>
              </article>
            </section>

            <section class="data-grid">
              <div class="page-card table-card">
                <h3>字段概况</h3>
                <el-table :data="csvAnalysis.profiles" size="small" max-height="420">
                  <el-table-column prop="name" label="字段" min-width="130" />
                  <el-table-column prop="type" label="类型" width="90" />
                  <el-table-column prop="count" label="有效值" width="85" />
                  <el-table-column prop="missing" label="缺失" width="75" />
                  <el-table-column prop="unique" label="唯一值" width="85" />
                </el-table>
              </div>
              <div class="page-card table-card">
                <h3>描述性统计</h3>
                <el-table :data="numericProfiles" size="small" max-height="420">
                  <el-table-column prop="name" label="数值字段" min-width="120" />
                  <el-table-column prop="statistics.mean" label="均值" width="95" />
                  <el-table-column prop="statistics.median" label="中位数" width="95" />
                  <el-table-column prop="statistics.min" label="最小" width="85" />
                  <el-table-column prop="statistics.max" label="最大" width="85" />
                  <el-table-column prop="statistics.stdev" label="标准差" width="95" />
                </el-table>
                <el-empty
                  v-if="!numericProfiles.length"
                  description="没有数值字段"
                  :image-size="52"
                />
              </div>
            </section>

            <section class="page-card chart-card">
              <div class="section-head">
                <div>
                  <h3>{{ csvAnalysis.chart.title }}</h3>
                  <p class="muted">{{ csvAnalysis.chart.caption }}</p>
                </div>
                <el-tag type="success">真实 CSV 聚合</el-tag>
              </div>
              <div ref="csvChartElement" class="csv-chart"></div>
              <p class="chart-analysis">{{ csvAnalysis.chart.analysis }}</p>
              <div class="data-provenance">
                <span>数据来源：{{ csvFile?.name || "本次上传文件" }}</span>
                <span class="mono">SHA-256：{{ csvAnalysis.chart.data_sha256 }}</span>
              </div>
              <p class="disclaimer">{{ csvAnalysis.disclaimer }}</p>
            </section>
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import * as echarts from "echarts";
import { ElMessage } from "element-plus";
import { UploadFilled } from "@element-plus/icons-vue";
import MultiPaperReviewView from "@/views/MultiPaperReviewView.vue";
import { fetchPapers } from "@/api/papers";
import {
  analyzeCsv,
  fetchVerifiableReferences,
  generateAcademicOutline,
  translateAcademicContent,
} from "@/api/writing";
import { ApiError } from "@/api/http";
import type { PaperListItem } from "@/types/paper";
import type {
  AcademicOutline,
  AcademicTranslation,
  CsvAnalysis,
  OutlineEvidence,
  TranslationDirection,
  VerifiableReferences,
} from "@/types/writing";
import { useTheme } from "@/composables/useTheme";
import { formatBytes } from "@/utils/format";

const router = useRouter();
const activeTab = ref("outline");
const papers = ref<PaperListItem[]>([]);

const outlinePaperId = ref<number | null>(null);
const outlineTopic = ref("");
const outline = ref<AcademicOutline | null>(null);
const outlineLoading = ref(false);
const outlineError = ref("");
const outlineElapsedMs = ref<number | null>(null);
const translationDirection = ref<TranslationDirection>("zh_to_en");
const translation = ref<AcademicTranslation | null>(null);
const translationLoading = ref(false);

const referencePaperIds = ref<number[]>([]);
const references = ref<VerifiableReferences | null>(null);
const referencesLoading = ref(false);
const referencesError = ref("");
const referencesElapsedMs = ref<number | null>(null);

const csvInput = ref<HTMLInputElement | null>(null);
const csvChartElement = ref<HTMLElement | null>(null);
const csvAnalysis = ref<CsvAnalysis | null>(null);
const csvFile = ref<File | null>(null);
const csvLoading = ref(false);
const csvError = ref("");
const csvElapsedMs = ref<number | null>(null);
let csvChart: echarts.ECharts | null = null;
const { theme } = useTheme();

const numericProfiles = computed(() =>
  (csvAnalysis.value?.profiles ?? []).filter(
    (profile) => profile.type === "integer" || profile.type === "number",
  ),
);

function paperLabel(paper: PaperListItem): string {
  return `${paper.title}${paper.publication_year ? ` (${paper.publication_year})` : ""}`;
}

async function createOutline() {
  if (!outlinePaperId.value) return;
  outlineLoading.value = true;
  outlineError.value = "";
  translation.value = null;
  outlineElapsedMs.value = null;
  const startedAt = performance.now();
  try {
    outline.value = await generateAcademicOutline(
      outlinePaperId.value,
      outlineTopic.value,
    );
  } catch (error) {
    outlineError.value = error instanceof ApiError
      ? error.message
      : "大纲生成失败";
  } finally {
    outlineElapsedMs.value = Math.round(performance.now() - startedAt);
    outlineLoading.value = false;
  }
}

function outlineSourceText(): string {
  if (!outline.value) return "";
  return outline.value.sections
    .map((section) => {
      const prefix = "#".repeat(section.level);
      const points = section.writing_points
        .map((point) => `- ${point}`)
        .join("\n");
      return `${prefix} ${section.heading}\n${points}`;
    })
    .join("\n\n");
}

async function translateOutline() {
  const source = outlineSourceText();
  if (!source) return;
  translationLoading.value = true;
  try {
    translation.value = await translateAcademicContent(
      source,
      translationDirection.value,
    );
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "翻译失败");
  } finally {
    translationLoading.value = false;
  }
}

function openEvidence(evidence: OutlineEvidence) {
  router.push({
    name: "paper-detail",
    params: { id: evidence.paper_id },
    query: {
      page: evidence.page_number,
    },
  });
}

async function loadReferences() {
  referencesLoading.value = true;
  referencesError.value = "";
  referencesElapsedMs.value = null;
  const startedAt = performance.now();
  try {
    references.value = await fetchVerifiableReferences(referencePaperIds.value);
  } catch (error) {
    referencesError.value = error instanceof ApiError
      ? error.message
      : "References 核验失败";
  } finally {
    referencesElapsedMs.value = Math.round(performance.now() - startedAt);
    referencesLoading.value = false;
  }
}

async function handleCsvFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  await analyzeSelectedCsv(file);
}

async function handleCsvDrop(event: DragEvent) {
  const file = event.dataTransfer?.files?.[0];
  if (file) await analyzeSelectedCsv(file);
}

async function analyzeSelectedCsv(file: File) {
  csvFile.value = file;
  csvLoading.value = true;
  csvError.value = "";
  csvElapsedMs.value = null;
  const startedAt = performance.now();
  try {
    csvAnalysis.value = await analyzeCsv(file);
    await nextTick();
    renderCsvChart();
  } catch (error) {
    csvAnalysis.value = null;
    csvChart?.dispose();
    csvChart = null;
    csvError.value = error instanceof ApiError
      ? error.message
      : "CSV 分析失败";
  } finally {
    csvElapsedMs.value = Math.round(performance.now() - startedAt);
    csvLoading.value = false;
  }
}

function retryCsvAnalysis() {
  if (csvFile.value) void analyzeSelectedCsv(csvFile.value);
}

function renderCsvChart() {
  if (!csvChartElement.value || !csvAnalysis.value) return;
  if (!csvChart) {
    csvChart = echarts.init(csvChartElement.value);
  }
  const chart = csvAnalysis.value.chart;
  const styles = getComputedStyle(document.documentElement);
  const cssValue = (name: string) => styles.getPropertyValue(name).trim();
  const textSecondary = cssValue("--color-text-secondary");
  const borderSubtle = cssValue("--color-border-subtle");
  csvChart.setOption({
    tooltip: {
      trigger: "axis",
      backgroundColor: cssValue("--color-surface-overlay"),
      borderColor: cssValue("--color-border-default"),
      textStyle: { color: cssValue("--color-text-primary") },
    },
    textStyle: { color: textSecondary },
    grid: { left: 60, right: 30, top: 30, bottom: 100 },
    xAxis: {
      type: "category",
      data: chart.categories,
      axisLabel: { rotate: 24, interval: 0, color: textSecondary },
      axisLine: { lineStyle: { color: borderSubtle } },
    },
    yAxis: {
      type: "value",
      name: chart.series_name,
      axisLabel: { color: textSecondary },
      splitLine: { lineStyle: { color: borderSubtle } },
    },
    series: [
      {
        type: "bar",
        name: chart.series_name,
        data: chart.values,
        barMaxWidth: 52,
        itemStyle: {
          color: cssValue("--color-primary"),
          borderRadius: [6, 6, 0, 0],
        },
      },
    ],
  }, true);
  csvChart.resize();
}

function resizeChart() {
  csvChart?.resize();
}

watch(activeTab, async (tab) => {
  if (tab === "data") {
    await nextTick();
    renderCsvChart();
  }
});

watch(theme, async () => {
  await nextTick();
  renderCsvChart();
});

onMounted(async () => {
  window.addEventListener("resize", resizeChart);
  try {
    papers.value = await fetchPapers();
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "加载论文列表失败");
  }
});

onUnmounted(() => {
  window.removeEventListener("resize", resizeChart);
  csvChart?.dispose();
  csvChart = null;
});
</script>

<style scoped>
.writing-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1380px;
  margin: 0 auto;
}

.writing-hero,
.result-cover,
.section-head,
.reference-summary,
.csv-stats {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.writing-hero {
  padding: 22px 24px;
}

.writing-hero h2,
.result-cover h2 {
  margin: 4px 0 6px;
}

.eyebrow {
  margin: 0;
  color: var(--accent-2);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.integrity-badges,
.translation-controls,
.unknown-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.writing-tabs {
  padding: 8px 18px 20px;
}

.tab-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-panel,
.result-cover,
.outline-section,
.translation-result,
.reference-card,
.table-card,
.chart-card {
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--color-surface);
}

.form-panel {
  max-width: 760px;
}

.request-meta {
  margin: 8px 0 0;
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 10px;
}

.loading-panel {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 20px;
  padding: 18px;
}

.loading-panel h3 {
  margin: 0 0 5px;
}

.loading-panel p {
  margin: 0;
}

.form-panel h3,
.section-head h3,
.table-card h3,
.chart-card h3 {
  margin: 0 0 10px;
}

.result-cover {
  flex-wrap: wrap;
  background: var(--color-primary-soft);
}

.disclaimer {
  width: 100%;
  margin: 4px 0 0;
  padding: 9px 12px;
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border-radius: 8px;
  font-size: 12px;
}

.content-labels,
.data-labels {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.content-labels article,
.data-labels article {
  padding: 12px 14px;
  border-left: 4px solid var(--accent);
  background: var(--color-bg-subtle);
  border-radius: 8px;
}

.content-labels strong,
.data-labels strong,
.content-labels span,
.data-labels span {
  display: block;
}

.content-labels span,
.data-labels span {
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
}

.outline-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.outline-section.child {
  margin-left: 34px;
}

.outline-section.unsupported {
  border-color: color-mix(in srgb, var(--color-warning) 32%, transparent);
  background: var(--color-warning-soft);
}

.outline-section ul {
  margin: 10px 0;
  padding-left: 22px;
  line-height: 1.75;
}

.unsupported-note,
.error-text {
  color: var(--color-warning);
}

.evidence-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.evidence-card {
  padding: 12px;
  background: var(--color-bg-subtle);
  border-radius: 10px;
}

.evidence-card p {
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.translation-result pre,
.reference-card pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 12px;
  background: var(--color-bg-subtle);
  border-radius: 8px;
}

.embedded-review {
  padding: 12px 0;
}

.reference-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 900px;
}

.reference-summary,
.csv-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(120px, 1fr));
  gap: 12px;
}

.csv-stats {
  grid-template-columns: repeat(4, minmax(120px, 1fr));
}

.reference-summary div,
.csv-stats div {
  padding: 16px;
  border-radius: 12px;
  background: var(--color-bg-subtle);
}

.reference-summary strong,
.reference-summary span,
.csv-stats strong,
.csv-stats span {
  display: block;
}

.reference-summary strong,
.csv-stats strong {
  font-size: 26px;
}

.reference-summary span,
.csv-stats span {
  color: var(--muted);
  font-size: 12px;
}

.reference-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.citation-line,
.chart-analysis {
  line-height: 1.7;
}

.reference-card blockquote {
  padding: 10px 12px;
  color: var(--color-text-secondary);
  background: var(--color-bg-subtle);
  border-left: 3px solid var(--accent);
}

.hidden-input {
  display: none;
}

.csv-dropzone {
  display: flex;
  width: 100%;
  min-height: 104px;
  align-items: center;
  gap: 14px;
  padding: 18px;
  color: var(--color-text-secondary);
  text-align: left;
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-lg);
  transition:
    border-color var(--duration-fast) var(--ease-standard),
    background var(--duration-fast) var(--ease-standard);
}

.csv-dropzone:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-primary);
}

.csv-dropzone:disabled {
  cursor: wait;
  opacity: 0.65;
}

.csv-dropzone > svg {
  width: 26px;
  flex: 0 0 auto;
  color: var(--color-primary);
}

.csv-dropzone > span {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}

.csv-dropzone strong {
  color: var(--color-text-primary);
  font-size: 13px;
}

.csv-dropzone small {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.csv-file-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 12px;
  margin-top: 9px;
  background: var(--color-primary-soft);
  border-radius: var(--radius-md);
  font-size: 10px;
}

.csv-file-info > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.csv-file-info span {
  color: var(--color-text-tertiary);
}

.csv-file-info strong {
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inline-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  margin-top: 10px;
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border-radius: var(--radius-md);
  font-size: 12px;
}

.inline-error :deep(.el-button) {
  margin-left: auto;
}

.data-provenance {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  padding: 9px 12px;
  color: var(--color-text-tertiary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
  font-size: 10px;
  overflow-wrap: anywhere;
}

.data-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.csv-chart {
  width: 100%;
  height: 430px;
}

@media (max-width: 980px) {
  .content-labels,
  .data-labels,
  .data-grid,
  .evidence-grid {
    grid-template-columns: 1fr;
  }

  .outline-section.child {
    margin-left: 0;
  }

  .csv-stats {
    grid-template-columns: repeat(2, minmax(120px, 1fr));
  }

  .loading-panel {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 620px) {
  .writing-hero {
    align-items: flex-start;
    flex-direction: column;
  }

  .csv-file-info {
    align-items: flex-start;
    flex-direction: column;
  }

  .csv-chart {
    height: 340px;
  }
}
</style>
