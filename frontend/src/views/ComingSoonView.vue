<template>
  <div class="page-card soon">
    <p class="stage">{{ stage }} · 尚未实现</p>
    <h2>{{ title }}</h2>
    <p>{{ feature }}</p>
    <p v-if="roadmap">{{ roadmap }}</p>
    <ul>
      <li>本页不会展示模拟问答、虚构检索结果或伪造引用。</li>
      <li>当前可用能力包括论文 PDF 管理与解析、混合检索，以及 v0.2.0 上下文感知与意图驱动问答。</li>
      <li>大规模知识库智能检索规划于 v0.3.0；科研知识图谱规划于 v0.4.0。</li>
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
const roadmap = computed(() => route.meta.roadmap as string | undefined);
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
