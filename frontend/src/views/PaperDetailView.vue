<template>
  <div class="detail" v-loading="loading">
    <el-button class="back" @click="router.push({ name: 'library' })">返回论文库</el-button>

    <template v-if="paper">
      <section class="page-card meta">
        <div>
          <h2>{{ paper.title }}</h2>
          <p class="muted">{{ paper.authors || "作者信息来自 PDF 元数据；若源文件未写入则显示为空。" }}</p>
        </div>
        <div class="stats">
          <div><span>页数</span><strong>{{ paper.page_count }}</strong></div>
          <div><span>字符</span><strong>{{ paper.char_count }}</strong></div>
          <div><span>大小</span><strong>{{ formatBytes(paper.file_size) }}</strong></div>
          <div><span>索引</span><strong>{{ statusLabel(paper.index_status) }}</strong></div>
        </div>
        <p class="muted filename">原始文件：{{ paper.original_filename }} · 上传于 {{ formatDateTime(paper.created_at) }}</p>
      </section>

      <section class="page-card reader">
        <div class="reader-head">
          <div>
            <h3>论文阅读器</h3>
            <p class="muted">结构内容来自已持久化解析结果；翻译仅用于辅助阅读。</p>
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
            <el-radio-button value="structured">结构视图</el-radio-button>
            <el-radio-button value="text">纯文本</el-radio-button>
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
            第 {{ current.page_number }} 页 · {{ current.char_count }} 字符 ·
            {{ statusLabel(current.parse_status) }}
          </header>
          <p v-if="current.ocr_error" class="ocr-error">{{ current.ocr_error }}</p>

          <div v-if="translationErrors[pageTranslationKey]" class="translation-error">
            <span>{{ translationErrors[pageTranslationKey] }}</span>
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
                :key="element.id"
                class="element-card"
                :class="`element-${element.element_type}`"
              >
                <header class="element-header">
                  <div class="element-heading">
                    <el-tag size="small" effect="plain">
                      {{ elementTypeLabel(element.element_type) }}
                    </el-tag>
                    <span class="element-index">#{{ element.element_index + 1 }}</span>
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

                <p v-if="element.section_path" class="section-path">
                  章节：{{ element.section_path }}
                </p>
                <p v-if="element.bbox" class="bbox">
                  布局坐标：{{ formatBbox(element.bbox) }}
                </p>

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

                <p
                  v-else
                  class="element-text"
                >
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
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
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
.back {
  margin-bottom: 14px;
}

.meta,
.reader {
  padding: 22px;
}

.meta {
  margin-bottom: 16px;
}

.meta h2 {
  margin: 0 0 8px;
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 18px 0 8px;
}

.stats div {
  background: #f4f8fb;
  border-radius: 12px;
  padding: 12px;
}

.stats span {
  display: block;
  color: var(--muted);
  font-size: 12px;
}

.filename {
  margin-bottom: 0;
}

.reader-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.reader-head h3 {
  margin: 0;
}

.page-text header {
  color: var(--muted);
  margin-bottom: 10px;
}

.page-text pre {
  white-space: pre-wrap;
  word-break: break-word;
  background: #f7fafc;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  min-height: 360px;
  line-height: 1.7;
}

.ocr-error {
  color: #b54708;
}

.reader-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 0 14px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 16px;
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
  background: #f7fafc;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  min-height: 220px;
  line-height: 1.7;
}

.page-translation {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.page-translation h4,
.element-translation h4 {
  margin: 0 0 8px;
}

.translation-disclaimer {
  grid-column: 1 / -1;
  color: #8a5b16;
  background: #fff7e8;
  border: 1px solid #f0d49f;
  border-radius: 8px;
  padding: 8px 10px;
  margin: 0;
  font-size: 12px;
}

.element-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.element-card {
  border: 1px solid var(--line);
  border-left: 4px solid #9bb7ca;
  border-radius: 10px;
  padding: 14px 16px;
  background: #fff;
}

.element-section {
  border-left-color: #c9842a;
  background: #fffaf2;
}

.element-table,
.element-formula {
  border-left-color: #4f91b7;
}

.element-reference {
  border-left-color: #8b9a77;
}

.element-header,
.element-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.element-index,
.section-path,
.bbox {
  color: var(--muted);
  font-size: 12px;
}

.section-path,
.bbox {
  margin: 8px 0 0;
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
  background: #f4f8fb;
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
  color: #b42318;
  background: #fff1f0;
  border: 1px solid #ffccc7;
  border-radius: 8px;
  padding: 8px 10px;
  margin: 12px 0;
}

@media (max-width: 900px) {
  .page-translation {
    grid-template-columns: 1fr;
  }

  .reader-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
