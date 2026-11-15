<template>
  <div class="page-card library">
    <section class="uploader">
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
          <p class="muted">每个文件分别解析和索引；上传传输完成不代表后台处理已完成。</p>
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
      <p class="muted">文件大小按服务端配置校验；超限文件会单独显示失败原因，不影响其他文件。</p>
      <p v-if="uploading" class="muted">文件传输中并创建任务；解析与索引进度会在任务列表中显示。</p>
      <el-button
        type="primary"
        :disabled="!selectedFiles.length || uploading"
        :loading="uploading"
        @click="submitBatch"
      >
        创建导入任务（{{ selectedFiles.length }}）
      </el-button>
    </section>

    <section v-if="jobs.length" class="jobs">
      <h3>导入任务</h3>
      <el-table :data="jobs" row-key="id" size="small">
        <el-table-column label="任务" min-width="180">
          <template #default="{ row }">
            {{ row.id.slice(0, 8) }} · {{ statusLabel(row.status) }}
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
        <el-table-column type="expand">
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

    <section class="toolbar">
      <el-input v-model="keyword" placeholder="按标题、文件名或作者筛选当前列表" clearable />
      <el-button :loading="loading" @click="loadPapers">刷新</el-button>
      <el-button :loading="checkingIndex" @click="checkIndex">检查索引</el-button>
      <el-button :loading="rebuildingIndex" @click="rebuildIndex">审计并定向修复索引</el-button>
    </section>

    <el-table
      :data="filteredPapers"
      v-loading="loading"
      empty-text="论文库为空。请上传 PDF，不会展示任何模拟论文。"
    >
      <el-table-column prop="title" label="标题" min-width="280" />
      <el-table-column prop="authors" label="作者" min-width="160">
        <template #default="{ row }">{{ row.authors || "PDF 元数据中未提供" }}</template>
      </el-table-column>
      <el-table-column label="页数" width="80" prop="page_count" />
      <el-table-column label="大小" width="110">
        <template #default="{ row }">{{ formatBytes(row.file_size) }}</template>
      </el-table-column>
      <el-table-column label="解析" width="90">
        <template #default="{ row }">{{ statusLabel(row.parse_status) }}</template>
      </el-table-column>
      <el-table-column label="索引" width="90">
        <template #default="{ row }">
          <el-tag type="info" size="small">{{ statusLabel(row.index_status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="上传时间" width="180">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openPaper(row.id)">查看</el-button>
          <el-button link type="primary" @click="openVersionManager(row)">相似/版本</el-button>
          <el-button link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

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
const checkingIndex = ref(false);
const rebuildingIndex = ref(false);
const keyword = ref("");
const uploadRef = ref<UploadInstance>();
const fileList = ref<UploadUserFile[]>([]);
const jobs = ref<ImportJob[]>([]);
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
  if (!q) return papers.value;
  return papers.value.filter((item) =>
    [item.title, item.original_filename, item.authors ?? ""].join(" ").toLowerCase().includes(q),
  );
});

async function loadPapers() {
  loading.value = true;
  try {
    papers.value = await fetchPapers();
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "加载论文列表失败";
    ElMessage.error(message);
  } finally {
    loading.value = false;
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

async function submitBatch() {
  if (!selectedFiles.value.length) return;
  uploading.value = true;
  try {
    const job = await createImportJob(selectedFiles.value);
    ElMessage.success(`已创建导入任务，包含 ${job.total_count} 个文件。`);
    fileList.value = [];
    uploadRef.value?.clearFiles();
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
    if (value === "1") ElMessage.info("请在下方上传区域选择 PDF 文件");
  },
);
</script>

<style scoped>
.library {
  padding: 20px;
}

.uploader {
  margin-bottom: 18px;
}

.selected-files {
  margin: 8px 0 12px;
  padding-left: 20px;
}

.selected-files li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 4px 0;
  overflow-wrap: anywhere;
}

.upload-copy {
  padding: 12px;
}

.jobs {
  margin-bottom: 20px;
}

.jobs h3 {
  margin: 0 0 12px;
}

.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.version-diff {
  max-height: 280px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #f7fafc;
  padding: 12px;
}
</style>
