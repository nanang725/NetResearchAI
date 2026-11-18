<template>
  <div class="detail" v-loading="loading">
    <template v-if="paper">
      <section class="page-card document-hero">
        <div>
          <p class="eyebrow">PAPER READER · PERSISTED STRUCTURE</p>
          <h2>{{ paper.title }}</h2>
          <p class="authors">
            {{ paper.authors || "PDF 元数据中未提供作者信息" }}
          </p>
          <div class="metadata-tags">
            <el-tag v-if="paper.publication_year" effect="plain">
              {{ paper.publication_year }}
            </el-tag>
            <el-tag v-if="paper.venue" effect="plain">{{ paper.venue }}</el-tag>
            <el-tag :type="paper.index_status === 'indexed' ? 'success' : 'info'" effect="plain">
              索引 {{ statusLabel(paper.index_status) }}
            </el-tag>
          </div>
        </div>
        <div class="hero-actions">
          <span class="filename mono">{{ paper.original_filename }}</span>
          <el-button plain :icon="Back" @click="router.push({ name: 'library' })">
            返回论文库
          </el-button>
        </div>
      </section>

      <div class="reader-layout">
        <section class="page-card reader">
          <div class="reader-head">
            <div>
              <p class="eyebrow">READING SURFACE</p>
              <h3>第 {{ currentPage }} / {{ paper.page_count }} 页</h3>
              <p class="muted">
                结构内容来自已持久化解析结果；翻译仅用于辅助阅读。
              </p>
            </div>
            <el-pagination
              v-if="paper.pages.length"
              layout="prev, pager, next, jumper"
              :page-size="1"
              :total="paper.pages.length"
              v-model:current-page="currentPage"
              background
            />
          </div>

          <div v-if="current" class="reader-toolbar">
            <el-radio-group v-model="viewMode" size="small">
              <el-radio-button value="structured">结构化阅读</el-radio-button>
              <el-radio-button value="text">纯文本阅读</el-radio-button>
            </el-radio-group>

            <div class="translation-controls">
              <el-select v-model="translationDirection" size="small" style="width: 150px">
                <el-option label="英文 → 中文" value="en_to_zh" />
                <el-option label="中文 → 英文" value="zh_to_en" />
              </el-select>
              <el-button
                type="primary"
                size="small"
                :loading="translationLoading[pageTranslationKey]"
                @click="translatePage"
              >
                翻译整页
              </el-button>
            </div>
          </div>

          <article v-if="current" class="page-content">
            <header class="page-header">
              <div>
                <strong>Page {{ current.page_number }}</strong>
                <span>{{ current.char_count }} 字符 · {{ statusLabel(current.parse_status) }}</span>
              </div>
              <el-tag :type="current.parse_status === 'parsed' ? 'success' : 'warning'" effect="plain">
                {{ statusLabel(current.parse_status) }}
              </el-tag>
            </header>
            <div v-if="current.ocr_error" class="reader-alert warning">
              {{ current.ocr_error }}
            </div>

            <div v-if="translationErrors[pageTranslationKey]" class="translation-error">
              <span>整页翻译失败：{{ translationErrors[pageTranslationKey] }}</span>
              <el-button link type="primary" @click="translatePage">重试</el-button>
            </div>
            <section
              v-if="translations[pageTranslationKey]"
              class="page-translation"
            >
              <div>
                <h4>页面原文</h4>
                <pre>{{ translations[pageTranslationKey].original_text }}</pre>
              </div>
              <div>
                <h4>{{ translations[pageTranslationKey].target_language }}译文</h4>
                <pre>{{ translations[pageTranslationKey].translated_text }}</pre>
              </div>
              <p class="translation-disclaimer">
                {{ translations[pageTranslationKey].disclaimer }}
              </p>
            </section>

            <pre v-if="viewMode === 'text'">{{ current.text || `（本页没有可用文本：${statusLabel(current.parse_status)}。）` }}</pre>

            <template v-else>
              <div v-if="orderedElements.length" class="element-list">
                <section
                  v-for="element in orderedElements"
                  :id="`element-${element.id}`"
                  :key="element.id"
                  class="element-card"
                  :class="`element-${element.element_type}`"
                >
                  <header class="element-header">
                    <div class="element-heading">
                      <el-tag size="small" effect="plain">
                        {{ elementTypeLabel(element.element_type) }}
                      </el-tag>
                      <span class="element-index">
                        Page {{ current.page_number }} · #{{ element.element_index + 1 }}
                      </span>
                    </div>
                    <el-button
                      v-if="element.text.trim()"
                      link
                      type="primary"
                      :loading="translationLoading[elementTranslationKey(element.id)]"
                      @click="translateElement(element)"
                    >
                      翻译此段
                    </el-button>
                  </header>

                  <div v-if="element.section_path || element.bbox" class="element-locator">
                    <span v-if="element.section_path">{{ element.section_path }}</span>
                    <span v-if="element.bbox" class="mono">bbox {{ formatBbox(element.bbox) }}</span>
                  </div>

                  <h4 v-if="element.element_type === 'section'" class="section-title">
                    {{ element.text }}
                  </h4>

                  <table
                    v-else-if="element.element_type === 'table' && tableRows(element).length"
                    class="table-preview"
                  >
                    <tbody>
                      <tr v-for="(row, rowIndex) in tableRows(element)" :key="rowIndex">
                        <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
                      </tr>
                    </tbody>
                  </table>

                  <div v-else-if="element.element_type === 'figure'" class="figure-meta">
                    图像对象
                    <span v-if="figureMetadata(element)"> · {{ figureMetadata(element) }}</span>
                  </div>

                  <pre
                    v-else-if="element.element_type === 'formula'"
                    class="formula-preview"
                  >{{ element.text }}</pre>

                  <p v-else class="element-text">
                    {{ element.text || "该元素没有可显示文本。" }}
                  </p>

                  <div
                    v-if="translationErrors[elementTranslationKey(element.id)]"
                    class="translation-error"
                  >
                    <span>{{ translationErrors[elementTranslationKey(element.id)] }}</span>
                    <el-button link type="primary" @click="translateElement(element)">重试</el-button>
                  </div>
                  <section
                    v-if="translations[elementTranslationKey(element.id)]"
                    class="element-translation"
                  >
                    <h4>{{ translations[elementTranslationKey(element.id)].target_language }}译文</h4>
                    <pre>{{ translations[elementTranslationKey(element.id)].translated_text }}</pre>
                    <p class="translation-disclaimer">
                      {{ translations[elementTranslationKey(element.id)].disclaimer }}
                    </p>
                  </section>
                </section>
              </div>
              <el-empty
                v-else
                description="当前页面没有结构化元素，可切换到纯文本视图。"
              />
            </template>
          </article>
        </section>

        <aside class="context-column">
          <section class="page-card context-card">
            <div class="context-heading">
              <div>
                <p class="eyebrow">DOCUMENT</p>
                <h3>阅读信息</h3>
              </div>
              <el-tag :type="translationState.type" effect="plain">
                {{ translationState.label }}
              </el-tag>
            </div>
            <div class="document-stats">
              <div><span>页数</span><strong>{{ paper.page_count }}</strong></div>
              <div><span>字符</span><strong>{{ compactNumber(paper.char_count) }}</strong></div>
              <div><span>大小</span><strong>{{ formatBytes(paper.file_size) }}</strong></div>
              <div><span>元素</span><strong>{{ orderedElements.length }}</strong></div>
            </div>
            <dl class="document-facts">
              <div>
                <dt>原始文件</dt>
                <dd class="mono">{{ paper.original_filename }}</dd>
              </div>
              <div>
                <dt>上传时间</dt>
                <dd>{{ formatDateTime(paper.created_at) }}</dd>
              </div>
              <div>
                <dt>翻译方向</dt>
                <dd>{{ translationDirection === "en_to_zh" ? "英文 → 中文" : "中文 → 英文" }}</dd>
              </div>
            </dl>
          </section>

          <section v-if="current" class="page-card context-card structure-card">
            <div class="context-heading">
              <div>
                <p class="eyebrow">STRUCTURE</p>
                <h3>本页结构</h3>
              </div>
              <span class="context-count">{{ currentSections.length }} 节</span>
            </div>
            <div v-if="currentSections.length" class="section-jump-list">
              <button
                v-for="element in currentSections"
                :key="element.id"
                type="button"
                @click="scrollToElement(element.id)"
              >
                <span>{{ element.section_path || element.text.slice(0, 30) }}</span>
                <small>#{{ element.element_index + 1 }}</small>
              </button>
            </div>
            <el-empty
              v-else
              description="本页未解析出章节标题"
              :image-size="54"
            />
            <div class="location-legend">
              <span>证据定位包含页码、结构序号与可用布局坐标。</span>
            </div>
          </section>
        </aside>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Back } from "@element-plus/icons-vue";
import { fetchPaper, translatePaperText } from "@/api/papers";
import { ApiError } from "@/api/http";
import type {
  PaperDetail,
  PaperElement,
  TranslationDirection,
  TranslationResult,
} from "@/types/paper";
import { formatBytes, formatDateTime, statusLabel } from "@/utils/format";
import {
  buildTranslationRequest,
  orderedPageElements,
  translationScopeKey,
} from "@/utils/translationReader";

const route = useRoute();
const router = useRouter();
const paper = ref<PaperDetail | null>(null);
const loading = ref(false);
const currentPage = ref(1);
const viewMode = ref<"structured" | "text">("structured");
const translationDirection = ref<TranslationDirection>("en_to_zh");
const translations = ref<Record<string, TranslationResult>>({});
const translationErrors = ref<Record<string, string>>({});
const translationLoading = ref<Record<string, boolean>>({});
let translationGeneration = 0;

const current = computed(
  () => paper.value?.pages.find((item) => item.page_number === currentPage.value) ?? null,
);
const orderedElements = computed(() => orderedPageElements(current.value?.elements ?? []));
const pageTranslationKey = computed(() =>
  translationScopeKey(currentPage.value),
);
const currentSections = computed(() =>
  orderedElements.value.filter(
    (element) => element.element_type === "section" && element.text.trim(),
  ),
);
const translationState = computed<{
  label: string;
  type: "success" | "warning" | "danger" | "info";
}>(() => {
  const key = pageTranslationKey.value;
  if (translationLoading.value[key]) {
    return { label: "整页翻译中", type: "warning" };
  }
  if (translationErrors.value[key]) {
    return { label: "翻译失败", type: "danger" };
  }
  if (translations.value[key]) {
    return { label: "整页已翻译", type: "success" };
  }
  return { label: "未翻译", type: "info" };
});

function elementTranslationKey(elementId: number): string {
  return translationScopeKey(currentPage.value, elementId);
}

function resetTranslations() {
  translationGeneration += 1;
  translations.value = {};
  translationErrors.value = {};
  translationLoading.value = {};
}

function elementTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    section: "章节",
    paragraph: "段落",
    table: "表格",
    formula: "公式",
    figure: "图像",
    figure_caption: "图注",
    reference: "参考文献",
  };
  return labels[type] ?? type;
}

function tableRows(element: PaperElement): string[][] {
  const data = element.data;
  if (!data || Array.isArray(data) || typeof data !== "object") return [];
  const rows = (data as Record<string, unknown>).rows;
  if (!Array.isArray(rows)) return [];
  return rows.map((row) =>
    Array.isArray(row) ? row.map((cell) => String(cell ?? "")) : [],
  );
}

function figureMetadata(element: PaperElement): string {
  const data = element.data;
  if (!data || Array.isArray(data) || typeof data !== "object") return "";
  const values = data as Record<string, unknown>;
  const dimensions =
    values.width && values.height
      ? `${String(values.width)} × ${String(values.height)}`
      : "";
  const xref = values.xref ? `xref ${String(values.xref)}` : "";
  return [dimensions, xref].filter(Boolean).join(" · ");
}

function formatBbox(bbox: number[]): string {
  return bbox.map((value) => Number(value).toFixed(1)).join(", ");
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    notation: value >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}

function scrollToElement(elementId: number) {
  document.getElementById(`element-${elementId}`)?.scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

async function requestTranslation(
  key: string,
  elementId?: number,
) {
  if (!paper.value || !current.value) return;
  const requestGeneration = translationGeneration;
  translationLoading.value = { ...translationLoading.value, [key]: true };
  translationErrors.value = { ...translationErrors.value, [key]: "" };
  try {
    const result = await translatePaperText(
      paper.value.id,
      buildTranslationRequest(
        current.value.page_number,
        translationDirection.value,
        elementId,
      ),
    );
    if (requestGeneration !== translationGeneration) return;
    translations.value = { ...translations.value, [key]: result };
  } catch (error) {
    if (requestGeneration !== translationGeneration) return;
    const message = error instanceof ApiError ? error.message : "翻译失败，请稍后重试。";
    translationErrors.value = { ...translationErrors.value, [key]: message };
  } finally {
    if (requestGeneration === translationGeneration) {
      translationLoading.value = { ...translationLoading.value, [key]: false };
    }
  }
}

function translatePage() {
  void requestTranslation(pageTranslationKey.value);
}

function translateElement(element: PaperElement) {
  if (!element.text.trim()) return;
  void requestTranslation(elementTranslationKey(element.id), element.id);
}

async function loadDetail(id: number) {
  loading.value = true;
  resetTranslations();
  try {
    paper.value = await fetchPaper(id);
    const requestedPage = Number(route.query.page);
    currentPage.value = (
      Number.isInteger(requestedPage)
      && requestedPage >= 1
      && requestedPage <= paper.value.pages.length
    )
      ? requestedPage
      : 1;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "加载论文详情失败";
    ElMessage.error(message);
    router.push({ name: "library" });
  } finally {
    loading.value = false;
  }
}

watch([currentPage, translationDirection], resetTranslations);

watch(
  () => route.query.page,
  (value) => {
    const requestedPage = Number(value);
    if (
      paper.value
      && Number.isInteger(requestedPage)
      && requestedPage >= 1
      && requestedPage <= paper.value.pages.length
    ) {
      currentPage.value = requestedPage;
    }
  },
);

onMounted(() => {
  const id = Number(route.params.id);
  if (!Number.isInteger(id)) {
    ElMessage.error("无效的论文编号");
    router.push({ name: "library" });
    return;
  }
  void loadDetail(id);
});

watch(
  () => route.params.id,
  (value) => {
    const id = Number(value);
    if (Number.isInteger(id)) void loadDetail(id);
  },
);
</script>

<style scoped>
.detail {
  min-width: 0;
}

.document-hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 24px;
  margin-bottom: 16px;
}

.document-hero > div:first-child {
  min-width: 0;
}

.document-hero h2 {
  max-width: 920px;
  margin: 5px 0 8px;
  font-size: clamp(22px, 2.5vw, 31px);
  line-height: 1.3;
  letter-spacing: -0.03em;
}

.authors {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.metadata-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 14px;
}

.hero-actions {
  display: flex;
  max-width: 240px;
  flex: 0 0 auto;
  align-items: flex-end;
  gap: 10px;
  flex-direction: column;
}

.filename {
  overflow: hidden;
  max-width: 100%;
  color: var(--color-text-tertiary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.reader-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 310px;
  align-items: start;
  gap: 16px;
}

.reader {
  min-width: 0;
  padding: 0;
  overflow: clip;
}

.reader-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 20px 22px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.reader-head h3 {
  margin: 3px 0;
  font-size: 18px;
}

.reader-head p {
  margin: 0;
}

.reader-toolbar {
  position: sticky;
  top: var(--layout-topbar-height);
  z-index: 4;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 11px 22px;
  background: var(--color-bg-subtle);
  border-bottom: 1px solid var(--color-border-subtle);
  backdrop-filter: blur(12px);
}

.translation-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.page-content > pre,
.page-translation pre,
.element-translation pre {
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--color-bg-subtle);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  min-height: 220px;
  line-height: 1.7;
}

.page-content {
  padding: 22px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 14px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.page-header > div {
  display: flex;
  flex-direction: column;
}

.page-header strong {
  font-size: 14px;
}

.page-header span {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.reader-alert {
  padding: 10px 12px;
  margin-bottom: 14px;
  border-radius: var(--radius-md);
  font-size: 12px;
}

.reader-alert.warning {
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border: 1px solid color-mix(in srgb, var(--color-warning) 24%, transparent);
}

.page-translation {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 20px;
}

.page-translation h4,
.element-translation h4 {
  margin: 0 0 8px;
}

.translation-disclaimer {
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border: 1px solid color-mix(in srgb, var(--color-warning) 24%, transparent);
  border-radius: 8px;
  padding: 8px 10px;
  margin: 0;
  font-size: 12px;
}

.element-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.element-card {
  scroll-margin-top: 150px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--color-border-strong);
  border-radius: 10px;
  padding: 16px;
  background: var(--color-surface);
}

.element-section {
  border-left-color: var(--color-accent);
  background: var(--color-accent-soft);
}

.element-table,
.element-formula {
  border-left-color: var(--color-info);
}

.element-reference {
  border-left-color: var(--color-success);
}

.element-header,
.element-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.element-index {
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 10px;
}

.element-locator {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  padding: 7px 9px;
  margin-top: 10px;
  color: var(--color-text-tertiary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-sm);
  font-size: 10px;
}

.section-title,
.element-text,
.figure-meta {
  margin: 12px 0 0;
  line-height: 1.75;
}

.table-preview {
  width: 100%;
  border-collapse: collapse;
  margin-top: 12px;
  font-size: 13px;
}

.table-preview td {
  border: 1px solid var(--line);
  padding: 7px 9px;
  vertical-align: top;
}

.formula-preview {
  margin-top: 12px;
  overflow-x: auto;
  background: var(--color-bg-subtle);
  padding: 12px;
  border-radius: 8px;
}

.element-translation {
  margin-top: 14px;
  border-top: 1px dashed var(--line);
  padding-top: 12px;
}

.translation-error {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border: 1px solid color-mix(in srgb, var(--color-danger) 24%, transparent);
  border-radius: 8px;
  padding: 8px 10px;
  margin: 12px 0;
}

.context-column {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.context-card {
  padding: 18px;
}

.context-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.context-heading h3 {
  margin: 3px 0 0;
  font-size: 16px;
}

.context-count {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.document-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.document-stats div {
  display: flex;
  padding: 10px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
  flex-direction: column;
}

.document-stats span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.document-stats strong {
  overflow: hidden;
  font-size: 15px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.document-facts {
  margin: 14px 0 0;
}

.document-facts > div {
  padding: 9px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.document-facts dt {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.document-facts dd {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.section-jump-list {
  display: flex;
  max-height: 300px;
  overflow-y: auto;
  flex-direction: column;
}

.section-jump-list button {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 0;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-top: 1px solid var(--color-border-subtle);
}

.section-jump-list button:first-child {
  border-top: 0;
}

.section-jump-list button:hover span {
  color: var(--color-primary);
}

.section-jump-list span {
  overflow: hidden;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.section-jump-list small {
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 9px;
}

.location-legend {
  padding: 10px;
  margin-top: 12px;
  color: var(--color-text-tertiary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-sm);
  font-size: 10px;
  line-height: 1.55;
}

@media (max-width: 900px) {
  .document-hero,
  .reader-head,
  .reader-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .hero-actions {
    width: 100%;
    max-width: none;
    align-items: flex-start;
  }

  .reader-layout {
    grid-template-columns: 1fr;
  }

  .reader-toolbar {
    position: static;
  }

  .page-translation {
    gap: 12px;
  }

  .context-column {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 620px) {
  .page-content,
  .reader-head,
  .reader-toolbar {
    padding: 15px;
  }

  .context-column {
    grid-template-columns: 1fr;
  }

  .translation-controls {
    width: 100%;
    align-items: stretch;
    flex-direction: column;
  }

  .translation-controls :deep(.el-select) {
    width: 100% !important;
  }
}
</style>
