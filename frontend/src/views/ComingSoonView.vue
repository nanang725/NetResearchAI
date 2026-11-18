<template>
  <div class="soon-page">
    <section class="page-card soon">
      <div class="soon-status">
        <span class="status-dot"></span>
        {{ stage }}
      </div>
      <div class="soon-icon">
        <Clock />
      </div>
      <h2>{{ title }}</h2>
      <p class="soon-description">{{ feature }}</p>
      <p v-if="roadmap" class="roadmap">{{ roadmap }}</p>
      <div class="capability-note">
        <strong>能力状态说明</strong>
        <ul>
          <li>当前产品版本为 v0.5.0，本页不会展示模拟结果或伪造能力。</li>
          <li>论文版本关系、差异查看和保留管理已在论文库提供。</li>
          <li>独立版本比较工作台尚未交付；科研知识图谱已提供可追溯的 v0.5.0 预览。</li>
        </ul>
      </div>
      <div class="soon-actions">
        <RouterLink to="/library">
          <el-button type="primary">前往论文库使用现有版本能力</el-button>
        </RouterLink>
        <RouterLink to="/">
          <el-button plain>返回科研工作台</el-button>
        </RouterLink>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { Clock } from "@element-plus/icons-vue";

const route = useRoute();
const title = computed(() => (route.meta.title as string) || "功能开发中");
const stage = computed(() => (route.meta.stage as string) || "后续阶段");
const feature = computed(() => (route.meta.feature as string) || "该功能尚未开发。");
const roadmap = computed(() => route.meta.roadmap as string | undefined);
</script>

<style scoped>
.soon-page {
  max-width: 820px;
  margin: 0 auto;
}

.soon {
  padding: 34px;
}

.soon-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--color-warning);
  font-size: 11px;
  font-weight: 650;
}

.status-dot {
  width: 7px;
  height: 7px;
  background: var(--color-warning);
  border-radius: 50%;
  box-shadow: 0 0 0 4px var(--color-warning-soft);
}

.soon-icon {
  display: grid;
  width: 48px;
  height: 48px;
  margin: 28px 0 18px;
  place-items: center;
  color: var(--color-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-lg);
}

.soon-icon svg {
  width: 23px;
}

.soon h2 {
  margin: 0 0 10px;
  font-size: 29px;
  letter-spacing: -0.035em;
}

.soon-description {
  max-width: 640px;
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 15px;
  line-height: 1.7;
}

.roadmap {
  padding: 12px 14px;
  margin: 18px 0 0;
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border-radius: var(--radius-md);
}

.capability-note {
  padding-top: 20px;
  margin-top: 24px;
  border-top: 1px solid var(--color-border-subtle);
}

.capability-note strong {
  font-size: 13px;
}

.capability-note ul {
  padding-left: 20px;
  margin: 10px 0 0;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.8;
}

.soon-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 24px;
}

@media (max-width: 620px) {
  .soon {
    padding: 22px;
  }

  .soon-actions {
    flex-direction: column;
  }

  .soon-actions a,
  .soon-actions :deep(.el-button) {
    width: 100%;
  }
}
</style>
