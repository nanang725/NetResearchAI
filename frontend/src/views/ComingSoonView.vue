<template>
  <div class="page-card soon">
    <p class="stage">{{ stage }} · 尚未实现</p>
    <h2>{{ title }}</h2>
    <p>{{ feature }}</p>
    <ul>
      <li>本页不会展示模拟问答、虚构检索结果或伪造引用。</li>
      <li>当前可用能力仅限论文 PDF 上传、校验、解析、列表、详情与删除。</li>
      <li>后续会接入向量检索 + BM25、LangGraph Agent 与 NetworkX 引用图谱。</li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();
const title = computed(() => (route.meta.title as string) || "功能开发中");
const stage = computed(() => (route.meta.stage as string) || "后续阶段");
const feature = computed(() => (route.meta.feature as string) || "该功能尚未开发。");
</script>

<style scoped>
.soon {
  padding: 28px;
  max-width: 760px;
}

.stage {
  color: var(--accent-2);
  font-weight: 700;
}

ul {
  line-height: 1.8;
  color: var(--muted);
}
</style>
