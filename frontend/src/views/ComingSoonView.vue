<template>
  <div class="page-card soon">
    <p class="stage">{{ stage }} · 尚未实现</p>
    <h2>{{ title }}</h2>
    <p>{{ feature }}</p>
    <p v-if="roadmap">{{ roadmap }}</p>
    <ul>
      <li>当前产品版本为 v0.3.0 开发预览，本页不会展示模拟结果或伪造能力。</li>
      <li>论文版本关系、差异查看和保留管理已在论文库提供；独立版本比较工作台尚未实现。</li>
      <li>完整科研知识图谱规划于 v0.5.0，完整科研 Copilot 规划于 v1.0.0。</li>
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
