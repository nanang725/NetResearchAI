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
          <h3>按页文本</h3>
          <el-pagination
            v-if="paper.pages.length"
            layout="prev, pager, next, jumper"
            :page-size="1"
            :total="paper.pages.length"
            v-model:current-page="currentPage"
            background
          />
        </div>
        <article v-if="current" class="page-text">
          <header>第 {{ current.page_number }} 页 · {{ current.char_count }} 字符</header>
          <pre>{{ current.text || "（本页未提取到文本，可能为扫描件或图片页。OCR 尚未实现。）" }}</pre>
        </article>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { fetchPaper } from "@/api/papers";
import { ApiError } from "@/api/http";
import type { PaperDetail } from "@/types/paper";
import { formatBytes, formatDateTime, statusLabel } from "@/utils/format";

const route = useRoute();
const router = useRouter();
const paper = ref<PaperDetail | null>(null);
const loading = ref(false);
const currentPage = ref(1);

const current = computed(() => paper.value?.pages.find((item) => item.page_number === currentPage.value) ?? null);

async function loadDetail(id: number) {
  loading.value = true;
  try {
    paper.value = await fetchPaper(id);
    currentPage.value = 1;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "加载论文详情失败";
    ElMessage.error(message);
    router.push({ name: "library" });
  } finally {
    loading.value = false;
  }
}

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
</style>
