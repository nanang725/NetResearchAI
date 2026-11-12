<template>
  <div class="page-card library">
    <section class="uploader">
      <el-upload
        drag
        :auto-upload="false"
        :show-file-list="false"
        accept="application/pdf,.pdf"
        :disabled="uploading"
        @change="onFileChange"
      >
        <div class="upload-copy">
          <strong>将计算机网络论文 PDF 拖到此处，或点击选择文件</strong>
          <p class="muted">仅接受真实 PDF。服务端会校验文件头、大小，并用 PyMuPDF 按页提取文本。</p>
          <p v-if="uploading" class="muted">正在上传并解析… {{ progress }}%</p>
        </div>
      </el-upload>
    </section>

    <section class="toolbar">
      <el-input v-model="keyword" placeholder="按标题、文件名或作者筛选当前列表" clearable />
      <el-button :loading="loading" @click="loadPapers">刷新</el-button>
    </section>

    <el-table :data="filteredPapers" v-loading="loading" empty-text="论文库为空。请上传 PDF，不会展示任何模拟论文。">
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
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openPaper(row.id)">查看</el-button>
          <el-button link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox, type UploadFile } from "element-plus";
import { deletePaper, fetchPapers, uploadPaper } from "@/api/papers";
import { ApiError } from "@/api/http";
import type { PaperListItem } from "@/types/paper";
import { formatBytes, formatDateTime, statusLabel } from "@/utils/format";

const route = useRoute();
const router = useRouter();
const papers = ref<PaperListItem[]>([]);
const loading = ref(false);
const uploading = ref(false);
const progress = ref(0);
const keyword = ref("");

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

async function onFileChange(uploadFile: UploadFile) {
  const file = uploadFile.raw;
  if (!file) return;
  uploading.value = true;
  progress.value = 0;
  try {
    const paper = await uploadPaper(file, (value) => {
      progress.value = value;
    });
    ElMessage.success(`已解析《${paper.title}》，共 ${paper.page_count} 页`);
    await loadPapers();
    router.push({ name: "paper-detail", params: { id: paper.id } });
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "上传失败";
    ElMessage.error(message);
  } finally {
    uploading.value = false;
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

onMounted(loadPapers);

watch(
  () => route.query.upload,
  (value) => {
    if (value === "1") {
      ElMessage.info("请在下方上传区域选择 PDF 文件");
    }
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

.upload-copy {
  padding: 12px;
}

.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
</style>
