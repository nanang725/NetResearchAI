<template>
  <div class="library-page">
    <section class="library-header page-card">
      <div>
        <p class="eyebrow">LOCAL EVIDENCE LIBRARY</p>
        <h2>论文库</h2>
        <p class="muted">
          管理已解析 PDF、索引状态与版本关系。列表只展示当前工作区真实馆藏。
        </p>
      </div>
      <div class="header-actions">
        <el-button plain @click="maintenanceOpen = !maintenanceOpen">
          {{ maintenanceOpen ? "收起高级维护" : "高级维护" }}
        </el-button>
        <el-button type="primary" :icon="Upload" @click="uploadDialog = true">
          导入 PDF
        </el-button>
      </div>
    </section>

    <section class="library-stats">
      <article>
        <span>馆藏论文</span>
        <strong>{{ loading ? "—" : papers.length }}</strong>
        <small>{{ loading ? "正在读取馆藏" : "真实 PDF 记录" }}</small>
      </article>
      <article>
        <span>可用证据</span>
        <strong>{{ loading ? "—" : readyPaperCount }}</strong>
        <small>{{ loading ? "正在读取状态" : "解析且已索引" }}</small>
      </article>
      <article>
        <span>解析页数</span>
        <strong>{{ loading ? "—" : totalPageCount }}</strong>
        <small>{{ loading ? "正在读取页面" : "已持久化页面" }}</small>
      </article>
      <article>
        <span>活跃任务</span>
        <strong>{{ loading ? "—" : activeJobs.length }}</strong>
        <small>{{ loading ? "正在读取任务" : "排队或处理中" }}</small>
      </article>
    </section>

    <section v-if="maintenanceOpen" class="maintenance-panel page-card">
      <div>
        <p class="eyebrow">ADVANCED MAINTENANCE</p>
        <h3>索引一致性维护</h3>
        <p class="muted">
          维护操作会检查 SQLite 与向量库的一致性。普通阅读和检索不需要执行这些操作。
        </p>
      </div>
      <div class="maintenance-actions">
        <el-button :loading="checkingIndex" @click="checkIndex">检查索引一致性</el-button>
        <el-button :loading="rebuildingIndex" @click="rebuildIndex">
          审计并定向修复索引
        </el-button>
      </div>
    </section>

    <section v-if="jobs.length" class="jobs page-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">IMPORT QUEUE</p>
          <h3>导入与索引任务</h3>
          <p class="muted">
            {{ activeJobs.length ? `${activeJobs.length} 个任务正在处理` : "当前没有活跃任务" }}
          </p>
        </div>
        <el-button :loading="jobsLoading" @click="refreshJobs">刷新任务</el-button>
      </div>
      <el-table :data="jobs" row-key="id" size="small">
        <el-table-column label="任务" min-width="180">
          <template #default="{ row }">
            <div class="job-id">
              <strong>{{ row.kind === "index_rebuild" ? "索引修复" : "PDF 导入" }}</strong>
              <span class="mono">{{ row.id.slice(0, 8) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="jobStatusType(row.status)" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="170">
          <template #default="{ row }">
            <el-progress
              :percentage="row.progress"
              :status="row.status === 'failed' ? 'exception' : undefined"
            />
          </template>
        </el-table-column>
        <el-table-column label="完成/失败/等待/处理中/总数" width="220">
          <template #default="{ row }">
            {{ row.completed_count }} / {{ row.failed_count }} / {{ row.queued_count }} /
            {{ row.running_count }} / {{ row.total_count }}
          </template>
        </el-table-column>
        <el-table-column label="提交时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button
              v-if="hasRetryableItems(row)"
              link
              type="primary"
              @click="retryJob(row.id)"
            >
              重试可重试项
            </el-button>
          </template>
        </el-table-column>
        <el-table-column type="expand" width="40">
          <template #default="{ row }">
            <el-table :data="row.items" size="small">
              <el-table-column prop="original_filename" label="文件" min-width="220" />
              <el-table-column label="阶段" width="100">
                <template #default="{ row: item }">{{ statusLabel(item.current_stage) }}</template>
              </el-table-column>
              <el-table-column label="解析" width="110">
                <template #default="{ row: item }">{{ statusLabel(item.parse_status) }}</template>
              </el-table-column>
              <el-table-column label="索引" width="100">
                <template #default="{ row: item }">{{ statusLabel(item.index_status) }}</template>
              </el-table-column>
              <el-table-column label="进度" width="120">
                <template #default="{ row: item }">
                  <el-progress
                    :percentage="item.progress"
                    :status="item.status === 'failed' ? 'exception' : undefined"
                  />
                </template>
              </el-table-column>
              <el-table-column prop="error_message" label="错误" min-width="240" />
              <el-table-column label="操作" width="90">
                <template #default="{ row: item }">
                  <el-button
                    v-if="item.status === 'failed' && item.can_retry"
                    link
                    type="primary"
                    :loading="retryingItemId === `${row.id}:${item.id}`"
                    @click="retryItem(row.id, item.id)"
                  >
                    重试
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="library-workspace page-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">PAPER COLLECTION</p>
          <h3>馆藏文献</h3>
          <p class="muted">
            显示 {{ filteredPapers.length }} / {{ papers.length }} 篇
          </p>
        </div>
        <span class="selection-count" v-if="selectedPapers.length">
          已选择 {{ selectedPapers.length }} 篇
        </span>
      </div>

      <div class="toolbar">
        <el-input
          v-model="keyword"
          :prefix-icon="Search"
          clearable
          placeholder="搜索标题、文件名或作者"
        />
        <el-select v-model="statusFilter" aria-label="筛选状态">
          <el-option label="全部状态" value="all" />
          <el-option label="可用证据" value="ready" />
          <el-option label="处理中" value="processing" />
          <el-option label="需关注" value="attention" />
        </el-select>
        <el-select v-model="sortMode" aria-label="排序方式">
          <el-option label="最近上传" value="newest" />
          <el-option label="标题 A-Z" value="title" />
          <el-option label="页数优先" value="pages" />
        </el-select>
        <el-button :loading="loading" :icon="Refresh" @click="loadPapers">刷新</el-button>
      </div>

      <div v-if="selectedPapers.length" class="selection-toolbar">
        <span>批量操作仅作用于已选择的本地论文。</span>
        <div>
          <el-button size="small" @click="clearPaperSelection">取消选择</el-button>
          <el-button
            size="small"
            type="danger"
            plain
            :loading="batchDeleting"
            @click="onBatchDelete"
          >
            删除所选
          </el-button>
        </div>
      </div>

      <div v-if="loadError" class="inline-error">
        <span>{{ loadError }}</span>
        <el-button link type="primary" @click="loadPapers">重试</el-button>
      </div>

      <div class="table-wrap">
        <el-table
          ref="tableRef"
          :data="filteredPapers"
          row-key="id"
          v-loading="loading"
          @selection-change="onPaperSelectionChange"
        >
          <el-table-column type="selection" width="44" />
          <el-table-column label="论文" min-width="330">
            <template #default="{ row }">
              <button type="button" class="paper-title-cell" @click="openPaper(row.id)">
                <strong>{{ row.title }}</strong>
                <span>{{ row.authors || "PDF 元数据中未提供作者" }}</span>
              </button>
            </template>
          </el-table-column>
          <el-table-column label="状态" min-width="150">
            <template #default="{ row }">
              <div class="paper-statuses">
                <el-tag :type="paperStatusType(row.parse_status)" size="small">
                  {{ statusLabel(row.parse_status) }}
                </el-tag>
                <el-tag :type="paperStatusType(row.index_status)" size="small" effect="plain">
                  索引 {{ statusLabel(row.index_status) }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="页数 / 大小" width="120">
            <template #default="{ row }">
              <span class="table-meta">{{ row.page_count }} 页 · {{ formatBytes(row.file_size) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="上传时间" width="170">
            <template #default="{ row }">
              <span class="table-meta">{{ formatDateTime(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openPaper(row.id)">阅读</el-button>
              <el-button link type="primary" @click="openVersionManager(row)">版本</el-button>
              <el-button link type="danger" @click="onDelete(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div v-if="loading" class="table-loading-state">
              <el-skeleton :rows="4" animated />
            </div>
            <el-empty v-else description="没有符合条件的真实论文记录">
              <el-button v-if="!papers.length" type="primary" @click="uploadDialog = true">
                导入第一篇论文
              </el-button>
            </el-empty>
          </template>
        </el-table>
      </div>
    </section>

    <el-dialog v-model="uploadDialog" title="导入论文 PDF" width="760px">
      <section class="uploader">
        <p class="muted upload-help">
          每个文件分别解析和索引。文件传输完成不代表后台处理完成，进度会在任务队列中持续更新。
        </p>
        <el-upload
          ref="uploadRef"
          drag
          multiple
          :limit="20"
          :auto-upload="false"
          :file-list="fileList"
          accept="application/pdf,.pdf"
          :disabled="uploading"
          :on-change="onFilesChange"
          :on-remove="onFileRemove"
          :on-exceed="onFileExceed"
        >
          <div class="upload-copy">
            <strong>将 PDF 拖到此处，或点击选择单篇/多篇论文</strong>
            <p class="muted">单次最多 20 个文件；服务端会逐文件校验大小与格式。</p>
          </div>
        </el-upload>
        <ul v-if="fileList.length" class="selected-files">
          <li v-for="file in fileList" :key="file.uid">
            <span>{{ file.name }} · {{ formatBytes(file.raw?.size ?? file.size ?? 0) }}</span>
            <el-tag v-if="fileValidationError(file)" type="warning" size="small">
              {{ fileValidationError(file) }}
            </el-tag>
          </li>
        </ul>
        <p v-if="uploading" class="muted">
          文件传输中并创建任务；请保持页面打开，解析与索引进度会在任务队列显示。
        </p>
      </section>
      <template #footer>
        <el-button :disabled="uploading" @click="uploadDialog = false">取消</el-button>
        <el-button
          type="primary"
          :disabled="!selectedFiles.length || uploading"
          :loading="uploading"
          @click="submitBatch"
        >
          创建导入任务（{{ selectedFiles.length }}）
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="versionDialog" title="相似论文与版本管理" width="900px">
      <template v-if="selectedPaper">
        <p>当前论文：{{ selectedPaper.title }}</p>
        <h4>语义相似候选（仅供人工核对）</h4>
        <el-table :data="similarCandidates" size="small" empty-text="没有达到候选阈值的论文">
          <el-table-column prop="title" label="候选论文" min-width="220" />
          <el-table-column prop="match_reason" label="匹配依据" min-width="310" />
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button link type="primary" @click="registerVersion(row.paper_id)">
                登记为版本
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <h4>已登记版本</h4>
        <el-table :data="versions" size="small" empty-text="尚未建立版本关系">
          <el-table-column prop="version_number" label="版本" width="70" />
          <el-table-column prop="title" label="标题" min-width="190" />
          <el-table-column prop="source" label="来源" width="100" />
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.created_at_paper) }}</template>
          </el-table-column>
          <el-table-column label="保留" width="80">
            <template #default="{ row }">{{ row.retained ? "是" : "否" }}</template>
          </el-table-column>
          <el-table-column label="操作" width="190">
            <template #default="{ row }">
              <el-button link type="primary" @click="showVersionDiff(row.paper_id)">差异</el-button>
              <el-button v-if="!row.retained" link type="primary" @click="retainVersion(row.paper_id)">
                保留
              </el-button>
              <el-button link type="danger" @click="removeVersion(row.paper_id)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <pre v-if="versionDiff" class="version-diff">{{ versionDiff }}</pre>
        <p v-if="diffTruncated" class="muted">差异过长，已截断显示。</p>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ElMessage,
  ElMessageBox,
  type UploadFile,
  type UploadFiles,
  type UploadInstance,
  type UploadUserFile,
} from "element-plus";
import { Refresh, Search, Upload } from "@element-plus/icons-vue";
import {
  addPaperVersion,
  comparePaperVersions,
  createImportJob,
  deletePaper,
  deletePaperVersion,
  fetchImportJobs,
  fetchIndexConsistency,
  fetchPaperVersions,
  fetchPapers,
  fetchSimilarPapers,
  retainPaperVersion,
  requestIndexRebuild,
  retryImportItem,
  retryImportJob,
} from "@/api/papers";
import { ApiError } from "@/api/http";
import type { ImportJob, PaperListItem, PaperVersion, SimilarPaperCandidate } from "@/types/paper";
import { createTaskPoller } from "@/utils/taskPoller";
import { formatBytes, formatDateTime, statusLabel } from "@/utils/format";

const route = useRoute();
const router = useRouter();
const papers = ref<PaperListItem[]>([]);
const loading = ref(false);
const uploading = ref(false);
const jobsLoading = ref(false);
const checkingIndex = ref(false);
const rebuildingIndex = ref(false);
const uploadDialog = ref(route.query.upload === "1");
const maintenanceOpen = ref(false);
const loadError = ref("");
const keyword = ref("");
const statusFilter = ref<"all" | "ready" | "processing" | "attention">("all");
const sortMode = ref<"newest" | "title" | "pages">("newest");
const uploadRef = ref<UploadInstance>();
const tableRef = ref<{ clearSelection: () => void }>();
const fileList = ref<UploadUserFile[]>([]);
const jobs = ref<ImportJob[]>([]);
const selectedPapers = ref<PaperListItem[]>([]);
const batchDeleting = ref(false);
const versionDialog = ref(false);
const selectedPaper = ref<PaperListItem | null>(null);
const similarCandidates = ref<SimilarPaperCandidate[]>([]);
const versions = ref<PaperVersion[]>([]);
const versionDiff = ref("");
const diffTruncated = ref(false);
let viewMounted = false;
let showJobLoadError = false;
const retryingItemId = ref<string | null>(null);

const jobPoller = createTaskPoller({
  fetchValue: fetchImportJobs,
  isActive: (loadedJobs) =>
    loadedJobs.some((job) => job.status === "queued" || job.status === "running"),
  onValue: (loadedJobs) => {
    jobs.value = loadedJobs;
    showJobLoadError = false;
    if (!loadedJobs.some((job) => job.status === "queued" || job.status === "running")) {
      void loadPapers();
    }
  },
  onError: (error) => {
    if (!showJobLoadError) return;
    const message = error instanceof ApiError ? error.message : "加载导入任务失败";
    ElMessage.error(message);
    showJobLoadError = false;
  },
  intervalMs: 1500,
});

const selectedFiles = computed(() =>
  fileList.value.flatMap((item) => (item.raw ? [item.raw] : [])),
);

const filteredPapers = computed(() => {
  const q = keyword.value.trim().toLowerCase();
  const filtered = papers.value.filter((item) => {
    if (
      q
      && ![item.title, item.original_filename, item.authors ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(q)
    ) {
      return false;
    }
    if (statusFilter.value === "ready") {
      return item.parse_status === "parsed" && item.index_status === "indexed";
    }
    if (statusFilter.value === "processing") {
      return ["queued", "running", "parsing", "indexing", "pending"].includes(
        item.index_status,
      ) || ["queued", "running", "parsing", "indexing"].includes(item.parse_status);
    }
    if (statusFilter.value === "attention") {
      return ["failed", "partial", "ocr_failed", "ocr_unavailable", "ocr_no_text"]
        .includes(item.parse_status)
        || ["failed", "partial", "error"].includes(item.index_status);
    }
    return true;
  });
  return [...filtered].sort((left, right) => {
    if (sortMode.value === "title") {
      return left.title.localeCompare(right.title, "zh-CN");
    }
    if (sortMode.value === "pages") return right.page_count - left.page_count;
    return new Date(right.created_at).getTime() - new Date(left.created_at).getTime();
  });
});

const activeJobs = computed(() =>
  jobs.value.filter((job) => ["queued", "running"].includes(job.status)),
);
const readyPaperCount = computed(
  () =>
    papers.value.filter(
      (paper) => paper.parse_status === "parsed" && paper.index_status === "indexed",
    ).length,
);
const totalPageCount = computed(() =>
  papers.value.reduce((total, paper) => total + paper.page_count, 0),
);

async function loadPapers() {
  loading.value = true;
  loadError.value = "";
  try {
    papers.value = await fetchPapers();
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "加载论文列表失败";
    loadError.value = message;
    ElMessage.error(message);
  } finally {
    loading.value = false;
  }
}

async function refreshJobs() {
  jobsLoading.value = true;
  try {
    jobs.value = await fetchImportJobs();
    showJobLoadError = false;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "加载导入任务失败";
    ElMessage.error(message);
  } finally {
    jobsLoading.value = false;
  }
}

function loadJobs(showError = false) {
  showJobLoadError = showJobLoadError || showError;
  jobPoller.refresh();
}

function acceptJobUpdate(job: ImportJob) {
  if (!viewMounted) return;
  const index = jobs.value.findIndex((current) => current.id === job.id);
  if (index < 0) {
    jobs.value = [job, ...jobs.value].slice(0, 50);
  } else {
    const updatedJobs = [...jobs.value];
    updatedJobs[index] = job;
    jobs.value = updatedJobs;
  }
  loadJobs();
}

function setUniqueFiles(files: UploadFiles) {
  const seen = new Set<string>();
  const uniqueFiles = files.filter((file) => {
    if (!file.raw) return true;
    const key = `${file.raw.name}\0${file.raw.size}\0${file.raw.lastModified}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (uniqueFiles.length !== files.length) {
    ElMessage.warning("已忽略重复选择的文件。");
  }
  fileList.value = [...uniqueFiles];
}

function onFilesChange(_file: UploadFile, files: UploadFiles) {
  setUniqueFiles(files);
}

function onFileRemove(_file: UploadFile, files: UploadFiles) {
  setUniqueFiles(files);
}

function onFileExceed() {
  ElMessage.warning("单次最多选择 20 个 PDF 文件。");
}

function fileValidationError(file: UploadUserFile): string {
  if (!file.raw) return "文件不可读取";
  if (file.raw.size === 0) return "文件为空";
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    return "格式不符，提交后会作为独立失败项显示";
  }
  return "";
}

function hasRetryableItems(job: ImportJob): boolean {
  return job.items.some((item) => item.can_retry);
}

function paperStatusType(
  status: string,
): "success" | "warning" | "danger" | "info" {
  if (status === "indexed" || status === "parsed" || status === "succeeded") {
    return "success";
  }
  if (status === "failed" || status === "partial" || status === "ocr_failed") {
    return "danger";
  }
  if (
    ["queued", "running", "parsing", "indexing", "pending", "needs_ocr"].includes(status)
  ) {
    return "warning";
  }
  return "info";
}

function jobStatusType(
  status: string,
): "success" | "warning" | "danger" | "info" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "partial") return "danger";
  if (status === "queued" || status === "running") return "warning";
  return "info";
}

function onPaperSelectionChange(rows: PaperListItem[]) {
  selectedPapers.value = rows;
}

function clearPaperSelection() {
  tableRef.value?.clearSelection();
  selectedPapers.value = [];
}

async function submitBatch() {
  if (!selectedFiles.value.length) return;
  uploading.value = true;
  try {
    const job = await createImportJob(selectedFiles.value);
    ElMessage.success(`已创建导入任务，包含 ${job.total_count} 个文件。`);
    fileList.value = [];
    uploadRef.value?.clearFiles();
    uploadDialog.value = false;
    acceptJobUpdate(job);
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "创建导入任务失败";
    ElMessage.error(message);
  } finally {
    uploading.value = false;
  }
}

async function retryJob(id: string) {
  try {
    const job = await retryImportJob(id);
    ElMessage.success("失败文件已重新排队。");
    acceptJobUpdate(job);
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "重试任务失败";
    ElMessage.error(message);
  }
}

async function retryItem(jobId: string, itemId: string) {
  retryingItemId.value = `${jobId}:${itemId}`;
  try {
    const job = await retryImportItem(jobId, itemId);
    ElMessage.success("该文件已重新排队。");
    acceptJobUpdate(job);
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "重试文件失败";
    ElMessage.error(message);
  } finally {
    retryingItemId.value = null;
  }
}

async function checkIndex() {
  checkingIndex.value = true;
  try {
    const result = await fetchIndexConsistency();
    if (result.parse_incomplete_page_count > 0) {
      ElMessage.warning(
        `PDF 解析未完成：${result.parse_incomplete_page_count} 个页面，涉及论文 ${result.parse_affected_paper_ids.join(", ")}。请先重试解析，不能通过索引修复绕过。`,
      );
      return;
    }
    if (
      !result.consistent
      && (
        result.stale_vector_count > 0
        || result.unverifiable_vector_count > 0
      )
    ) {
      ElMessage.warning(
        `索引不一致：缺失 ${result.missing_vector_count} 个，过期 ${result.stale_vector_count} 个，孤立 ${result.orphan_vector_count} 个，无法验证 ${result.unverifiable_vector_count} 个。`,
      );
      return;
    }
    if (result.consistent) {
      ElMessage.success(`索引一致：${result.sqlite_chunk_count} 个文本块。`);
    } else {
      ElMessage.warning(
        `索引不一致：缺少 ${result.missing_vector_count} 个向量，存在 ${result.orphan_vector_count} 个孤立向量。`,
      );
    }
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "检查索引失败");
  } finally {
    checkingIndex.value = false;
  }
}

async function rebuildIndex() {
  try {
    await ElMessageBox.confirm(
      "将先执行内容感知审计，仅为缺失或过期向量创建定向修复任务；确认孤立的向量会被清理。",
      "重建向量索引",
      { type: "warning", confirmButtonText: "创建任务", cancelButtonText: "取消" },
    );
    rebuildingIndex.value = true;
    const job = await requestIndexRebuild();
    ElMessage.success("索引审计完成，定向修复任务已创建。");
    acceptJobUpdate(job);
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    ElMessage.error(error instanceof ApiError ? error.message : "创建定向索引修复任务失败");
  } finally {
    rebuildingIndex.value = false;
  }
}

async function openVersionManager(paper: PaperListItem) {
  selectedPaper.value = paper;
  versionDialog.value = true;
  similarCandidates.value = [];
  versions.value = [];
  versionDiff.value = "";
  diffTruncated.value = false;
  try {
    versions.value = await fetchPaperVersions(paper.id);
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "加载版本列表失败");
  }
  try {
    similarCandidates.value = await fetchSimilarPapers(paper.id);
  } catch (error) {
    ElMessage.warning(error instanceof ApiError ? error.message : "加载相似论文失败");
  }
}

async function registerVersion(candidateId: number) {
  if (!selectedPaper.value) return;
  try {
    await ElMessageBox.confirm(
      "确认将两篇论文登记为同一论文的不同版本？系统不会自动合并或删除内容。",
      "确认版本关系",
      { type: "warning", confirmButtonText: "登记", cancelButtonText: "取消" },
    );
    versions.value = await addPaperVersion(selectedPaper.value.id, candidateId);
    similarCandidates.value = similarCandidates.value.filter((item) => item.paper_id !== candidateId);
    ElMessage.success("版本关系已保存。");
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    ElMessage.error(error instanceof ApiError ? error.message : "登记版本失败");
  }
}

async function retainVersion(paperId: number) {
  if (!selectedPaper.value) return;
  try {
    versions.value = await retainPaperVersion(selectedPaper.value.id, paperId);
    ElMessage.success("已更新保留版本。");
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "保留版本失败");
  }
}

async function showVersionDiff(paperId: number) {
  if (!selectedPaper.value) return;
  try {
    const result = await comparePaperVersions(selectedPaper.value.id, paperId);
    versionDiff.value = result.diff;
    diffTruncated.value = result.truncated;
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "加载版本差异失败");
  }
}

async function removeVersion(paperId: number) {
  if (!selectedPaper.value) return;
  try {
    await ElMessageBox.confirm(
      "将删除该版本的论文记录、PDF 文件及其向量索引。",
      "删除论文版本",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    await deletePaperVersion(selectedPaper.value.id, paperId);
    versions.value = await fetchPaperVersions(selectedPaper.value.id);
    await loadPapers();
    ElMessage.success("已删除该版本。");
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    ElMessage.error(error instanceof ApiError ? error.message : "删除版本失败");
  }
}

function openPaper(id: number) {
  router.push({ name: "paper-detail", params: { id } });
}

async function onDelete(row: PaperListItem) {
  try {
    await ElMessageBox.confirm(`将删除《${row.title}》及其解析文本，且不可恢复。`, "删除论文", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await deletePaper(row.id);
    ElMessage.success("已删除");
    await loadPapers();
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    const message = error instanceof ApiError ? error.message : "删除失败";
    ElMessage.error(message);
  }
}

async function onBatchDelete() {
  if (!selectedPapers.value.length || batchDeleting.value) return;
  const count = selectedPapers.value.length;
  try {
    await ElMessageBox.confirm(
      `将删除所选的 ${count} 篇论文及其解析文本，且不可恢复。`,
      "批量删除论文",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }

  batchDeleting.value = true;
  const failures: string[] = [];
  for (const paper of selectedPapers.value) {
    try {
      await deletePaper(paper.id);
    } catch (error) {
      failures.push(
        `${paper.title}：${error instanceof ApiError ? error.message : "删除失败"}`,
      );
    }
  }
  await loadPapers();
  clearPaperSelection();
  batchDeleting.value = false;
  if (failures.length) {
    ElMessage.warning(`批量删除完成，${failures.length} 篇失败：${failures[0]}`);
  } else {
    ElMessage.success(`已删除 ${count} 篇论文。`);
  }
}

onMounted(() => {
  viewMounted = true;
  void loadPapers();
  showJobLoadError = true;
  jobPoller.start();
});

onUnmounted(() => {
  viewMounted = false;
  jobPoller.stop();
});

watch(
  () => route.query.upload,
  (value) => {
    uploadDialog.value = value === "1";
  },
);
</script>

<style scoped>
.library-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.library-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 24px;
}

.library-header h2 {
  margin: 4px 0 6px;
  font-size: 24px;
  letter-spacing: -0.03em;
}

.library-header p {
  max-width: 720px;
  margin: 0;
}

.header-actions,
.maintenance-actions,
.section-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.library-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.library-stats article {
  display: flex;
  min-width: 0;
  padding: 16px 18px;
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-lg);
  flex-direction: column;
}

.library-stats span {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.library-stats strong {
  margin: 3px 0 0;
  font-size: 25px;
  letter-spacing: -0.04em;
}

.library-stats small {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.maintenance-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 16px 20px;
  background: var(--color-warning-soft);
  border-color: color-mix(in srgb, var(--color-warning) 26%, transparent);
}

.maintenance-panel h3,
.jobs h3,
.library-workspace h3 {
  margin: 3px 0;
  font-size: 17px;
}

.maintenance-panel p,
.jobs p,
.library-workspace p {
  margin: 0;
}

.jobs,
.library-workspace {
  padding: 20px;
}

.library-workspace {
  order: 3;
}

.jobs {
  order: 4;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.job-id {
  display: flex;
  flex-direction: column;
}

.job-id span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.toolbar {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) 150px 150px auto;
  gap: 10px;
  margin-bottom: 12px;
}

.selection-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 12px;
  margin-bottom: 10px;
  color: var(--color-text-secondary);
  background: var(--color-primary-soft);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-md);
  font-size: 12px;
}

.selection-toolbar > div,
.paper-statuses {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.selection-count {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.inline-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  margin-bottom: 12px;
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border: 1px solid color-mix(in srgb, var(--color-danger) 24%, transparent);
  border-radius: var(--radius-md);
  font-size: 13px;
}

.inline-error :deep(.el-button) {
  margin-left: auto;
}

.table-wrap {
  min-width: 0;
  overflow: hidden;
}

.table-loading-state {
  padding: 22px;
}

.paper-title-cell {
  display: flex;
  width: 100%;
  min-width: 0;
  padding: 0;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  flex-direction: column;
}

.paper-title-cell strong {
  display: -webkit-box;
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.paper-title-cell span,
.table-meta {
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-help {
  margin: 0 0 12px;
  line-height: 1.65;
}

.upload-copy {
  padding: 12px;
}

.upload-copy strong {
  color: var(--color-text-primary);
}

.upload-copy p {
  margin: 6px 0 0;
}

.selected-files {
  max-height: 180px;
  padding: 0;
  margin: 12px 0 0;
  overflow-y: auto;
  list-style: none;
}

.selected-files li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 4px;
  border-top: 1px solid var(--color-border-subtle);
  overflow-wrap: anywhere;
}

.version-diff {
  max-height: 280px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--color-text-primary);
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-sm);
  padding: 12px;
}

@media (max-width: 900px) {
  .library-header,
  .maintenance-panel,
  .selection-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .library-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .toolbar {
    grid-template-columns: 1fr 1fr;
  }

  .toolbar :deep(.el-input) {
    grid-column: 1 / -1;
  }
}

@media (max-width: 560px) {
  .library-stats {
    grid-template-columns: 1fr;
  }

  .toolbar {
    grid-template-columns: 1fr;
  }

  .toolbar :deep(.el-input) {
    grid-column: auto;
  }

  .jobs,
  .library-workspace {
    padding: 16px;
  }
}
</style>
