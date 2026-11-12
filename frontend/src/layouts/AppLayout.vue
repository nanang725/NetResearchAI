<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="mark">NR</div>
        <div>
          <div class="brand-name">NetResearch AI</div>
          <div class="brand-sub">计算机网络科研助手</div>
        </div>
      </div>

      <nav class="nav">
        <RouterLink to="/" class="nav-item" exact-active-class="active">论文库</RouterLink>
        <RouterLink to="/qa" class="nav-item" active-class="active">智能问答</RouterLink>
        <RouterLink to="/graph" class="nav-item" active-class="active">引用图谱</RouterLink>
        <RouterLink to="/compare" class="nav-item" active-class="active">版本比较</RouterLink>
      </nav>

      <div class="sidebar-foot">
        <div class="health">
          <span class="dot" :class="healthOk ? 'ok' : 'bad'"></span>
          <span>{{ healthText }}</span>
        </div>
        <p>第一阶段：论文入库与 PDF 解析。问答、图谱与版本比较尚未实现。</p>
      </div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <h1>{{ pageTitle }}</h1>
          <p class="muted">真实论文数据工作台，不使用模拟检索或虚构引用。</p>
        </div>
        <el-button type="primary" @click="goUpload">上传论文 PDF</el-button>
      </header>
      <main class="content">
        <router-view />
      </main>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { fetchHealth } from "@/api/papers";

const route = useRoute();
const router = useRouter();
const healthOk = ref(false);
const healthText = ref("正在检查后端…");

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? "工作台");

function goUpload() {
  router.push({ name: "library", query: { upload: "1" } });
}

onMounted(async () => {
  try {
    const health = await fetchHealth();
    healthOk.value = health.status === "ok";
    healthText.value = `${health.name} ${health.version} 在线`;
  } catch {
    healthOk.value = false;
    healthText.value = "后端未连接";
  }
});
</script>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 260px 1fr;
  min-height: 100vh;
}

.sidebar {
  background: linear-gradient(180deg, #10283a 0%, #16384f 100%);
  color: #e8f1f6;
  padding: 24px 18px;
  display: flex;
  flex-direction: column;
  gap: 28px;
}

.brand {
  display: flex;
  gap: 12px;
  align-items: center;
}

.mark {
  width: 42px;
  height: 42px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  background: #c9842a;
  color: #10283a;
  font-weight: 800;
}

.brand-name {
  font-size: 16px;
  font-weight: 700;
}

.brand-sub {
  font-size: 12px;
  color: #9fb4c4;
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.nav-item {
  padding: 10px 12px;
  border-radius: 10px;
  color: #d5e4ee;
}

.nav-item.active,
.nav-item:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.sidebar-foot {
  margin-top: auto;
  font-size: 12px;
  color: #9fb4c4;
  line-height: 1.6;
}

.health {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  color: #e8f1f6;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #8aa0b0;
}

.dot.ok {
  background: #4ad39a;
}

.dot.bad {
  background: #ef6b5b;
}

.workspace {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.topbar {
  padding: 22px 28px 8px;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.topbar h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.content {
  padding: 12px 28px 32px;
}

@media (max-width: 900px) {
  .shell {
    grid-template-columns: 1fr;
  }
  .sidebar {
    display: none;
  }
}
</style>
