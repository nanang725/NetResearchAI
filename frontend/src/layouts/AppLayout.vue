<template>
  <div class="app-shell">
    <button
      v-if="mobileNavOpen"
      type="button"
      class="sidebar-backdrop"
      aria-label="关闭导航"
      @click="mobileNavOpen = false"
    ></button>

    <aside class="sidebar" :class="{ 'is-open': mobileNavOpen }">
      <div class="sidebar-head">
        <RouterLink to="/" class="brand" aria-label="NetResearch AI 研究台首页">
          <span class="brand-mark" aria-hidden="true">
            <i></i>
            <i></i>
            <i></i>
          </span>
          <span class="brand-copy">
            <strong>NetResearch AI</strong>
            <small>Research workspace</small>
          </span>
        </RouterLink>
        <button
          type="button"
          class="sidebar-close"
          aria-label="关闭导航"
          @click="mobileNavOpen = false"
        >
          <Close />
        </button>
      </div>

      <nav class="sidebar-nav" aria-label="主导航">
        <section v-for="section in navSections" :key="section.label" class="nav-section">
          <p class="nav-label">{{ section.label }}</p>
          <RouterLink
            v-for="item in section.items"
            :key="item.to"
            :to="item.to"
            class="nav-item"
            :class="{ 'is-active': isNavActive(item) }"
            :aria-current="isNavActive(item) ? 'page' : undefined"
            @click="mobileNavOpen = false"
          >
            <component :is="item.icon" class="nav-icon" />
            <span>{{ item.label }}</span>
            <el-tag
              v-if="item.badge"
              size="small"
              effect="plain"
              class="nav-badge"
            >
              {{ item.badge }}
            </el-tag>
          </RouterLink>
        </section>
      </nav>

      <div class="sidebar-foot">
        <div class="health-card">
          <span class="health-dot" :class="healthOk ? 'ok' : 'bad'"></span>
          <div>
            <strong>{{ healthText }}</strong>
            <small>服务连接状态</small>
          </div>
        </div>
        <div class="version-line">
          <span>{{ appVersion }}</span>
          <span>正式版本</span>
        </div>
        <p>真实论文与证据优先。v0.5.0 提供科研知识图谱与跨论文关联。</p>
      </div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div class="topbar-start">
          <button
            type="button"
            class="mobile-nav-toggle"
            aria-label="打开导航"
            :aria-expanded="mobileNavOpen"
            @click="mobileNavOpen = true"
          >
            <Menu />
          </button>
          <div class="page-context">
            <span>NetResearch / 研究工作台</span>
            <h1>{{ pageTitle }}</h1>
          </div>
        </div>

        <div class="topbar-actions">
          <span class="preview-badge">Evidence-first</span>
          <button
            type="button"
            class="theme-toggle"
            :aria-label="isDark ? '切换到浅色主题' : '切换到深色主题'"
            :title="isDark ? '切换到浅色主题' : '切换到深色主题'"
            :aria-pressed="isDark"
            @click="toggleTheme"
          >
            <Sunny v-if="isDark" />
            <Moon v-else />
            <span>{{ isDark ? "浅色" : "深色" }}</span>
          </button>
          <el-button type="primary" :icon="Upload" @click="goUpload">
            导入论文
          </el-button>
        </div>
      </header>

      <main class="content">
        <RouterView />
      </main>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch, type Component } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ChatDotRound,
  Close,
  Collection,
  Compass,
  Cpu,
  EditPen,
  Files,
  Grid,
  Menu,
  Moon,
  Search,
  Share,
  Sunny,
  Switch,
  Upload,
} from "@element-plus/icons-vue";
import { fetchHealth } from "@/api/papers";
import { useTheme } from "@/composables/useTheme";

interface NavItem {
  to: string;
  label: string;
  icon: Component;
  badge?: string;
  matchNames?: string[];
}

interface NavSection {
  label: string;
  items: NavItem[];
}

const route = useRoute();
const router = useRouter();
const healthOk = ref(false);
const healthText = ref("正在检查服务");
const appVersion = ref("v0.5.0");
const mobileNavOpen = ref(false);
const { isDark, toggleTheme } = useTheme();

const navSections: NavSection[] = [
  {
    label: "工作台",
    items: [{ to: "/", label: "科研工作台", icon: Grid }],
  },
  {
    label: "文献管理",
    items: [
      {
        to: "/library",
        label: "论文库",
        icon: Collection,
        matchNames: ["paper-detail"],
      },
      { to: "/search", label: "智能检索", icon: Search },
      {
        to: "/discovery",
        label: "每日论文推荐",
        icon: Compass,
        badge: "arXiv",
      },
    ],
  },
  {
    label: "研究分析",
    items: [
      { to: "/qa", label: "智能问答", icon: ChatDotRound },
      {
        to: "/knowledge-graph",
        label: "科研知识图谱",
        icon: Share,
        badge: "v0.5",
      },
      { to: "/graph", label: "引用图谱", icon: Share },
      { to: "/review", label: "多文献综述", icon: Files },
      { to: "/writing", label: "学术写作", icon: EditPen },
    ],
  },
  {
    label: "AI 研究",
    items: [
      {
        to: "/research-agent",
        label: "自主科研 Agent",
        icon: Cpu,
        badge: "预览",
      },
    ],
  },
  {
    label: "系统信息",
    items: [{ to: "/compare", label: "版本比较", icon: Switch }],
  },
];

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? "研究工作台");

function isNavActive(item: NavItem): boolean {
  if (item.to === "/") return route.path === "/";
  if (item.matchNames?.includes(String(route.name))) return true;
  return route.path === item.to || route.path.startsWith(`${item.to}/`);
}

function goUpload(): void {
  void router.push({ name: "library", query: { upload: "1" } });
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") mobileNavOpen.value = false;
}

watch(
  () => route.fullPath,
  () => {
    mobileNavOpen.value = false;
  },
);

onMounted(async () => {
  window.addEventListener("keydown", handleKeydown);
  try {
    const health = await fetchHealth();
    healthOk.value = health.status === "ok";
    appVersion.value = health.version.startsWith("v")
      ? health.version
      : `v${health.version}`;
    healthText.value = "服务在线";
  } catch {
    healthOk.value = false;
    healthText.value = "服务未连接";
  }
});

onUnmounted(() => {
  window.removeEventListener("keydown", handleKeydown);
});
</script>

<style scoped>
.app-shell {
  display: grid;
  min-height: 100vh;
  grid-template-columns: var(--layout-sidebar-width) minmax(0, 1fr);
  background: var(--color-bg-canvas);
}

.sidebar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  height: 100vh;
  min-height: 0;
  padding: 18px 12px 14px;
  color: var(--color-text-secondary);
  background: var(--color-sidebar);
  border-right: 1px solid var(--color-border-subtle);
  flex-direction: column;
}

.sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 8px 17px;
}

.brand {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.brand-mark {
  position: relative;
  display: block;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  background: var(--color-primary);
  border-radius: 10px;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, white 16%, transparent);
}

.brand-mark::before,
.brand-mark::after {
  position: absolute;
  height: 1px;
  content: "";
  background: color-mix(in srgb, white 72%, transparent);
  transform-origin: left center;
}

.brand-mark::before {
  top: 11px;
  left: 10px;
  width: 15px;
  transform: rotate(28deg);
}

.brand-mark::after {
  top: 23px;
  left: 10px;
  width: 16px;
  transform: rotate(-30deg);
}

.brand-mark i {
  position: absolute;
  z-index: 1;
  width: 5px;
  height: 5px;
  background: white;
  border-radius: 50%;
}

.brand-mark i:nth-child(1) {
  top: 8px;
  left: 8px;
}

.brand-mark i:nth-child(2) {
  top: 15px;
  right: 7px;
}

.brand-mark i:nth-child(3) {
  bottom: 6px;
  left: 9px;
}

.brand-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.brand-copy strong {
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: 14px;
  font-weight: 680;
  letter-spacing: -0.02em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.brand-copy small {
  color: var(--color-text-tertiary);
  font-size: 10px;
  letter-spacing: 0.02em;
}

.sidebar-close {
  display: none;
  width: 32px;
  height: 32px;
  padding: 7px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
}

.sidebar-nav {
  min-height: 0;
  flex: 1;
  padding: 0 0 14px;
  overflow-y: auto;
}

.nav-section + .nav-section {
  margin-top: 18px;
}

.nav-label {
  margin: 0 10px 7px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.08em;
}

.nav-item {
  position: relative;
  display: flex;
  min-height: 36px;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  border-radius: 7px;
  transition:
    color var(--duration-fast) var(--ease-standard),
    background var(--duration-fast) var(--ease-standard);
}

.nav-item:hover {
  color: var(--color-text-primary);
  background: var(--color-surface-hover);
}

.nav-item.is-active {
  color: var(--color-text-primary);
  font-weight: 600;
  background: var(--color-surface-active);
}

.nav-item.is-active::before {
  position: absolute;
  top: 10px;
  bottom: 10px;
  left: -12px;
  width: 2px;
  content: "";
  background: var(--color-primary);
  border-radius: 0 2px 2px 0;
}

.nav-icon {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
}

.nav-item.is-active .nav-icon {
  color: var(--color-primary);
}

.nav-badge {
  margin-left: auto;
  transform: scale(0.86);
  transform-origin: right center;
}

.sidebar-foot {
  padding: 12px 8px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.health-card {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.health-card > div {
  display: flex;
  flex-direction: column;
}

.health-card strong {
  color: var(--color-text-primary);
  font-size: 11px;
  font-weight: 600;
}

.health-card small {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.health-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  background: var(--color-text-tertiary);
  border-radius: 50%;
}

.health-dot.ok {
  background: var(--color-success);
  box-shadow: 0 0 0 3px var(--color-success-soft);
}

.health-dot.bad {
  background: var(--color-danger);
  box-shadow: 0 0 0 3px var(--color-danger-soft);
}

.version-line {
  display: flex;
  justify-content: space-between;
  margin: 10px 2px 4px;
  color: var(--color-text-secondary);
  font-family: var(--font-mono);
  font-size: 10px;
}

.sidebar-foot > p {
  margin: 0 2px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.55;
}

.workspace {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  min-height: var(--layout-topbar-height);
  align-items: center;
  justify-content: space-between;
  gap: var(--space-5);
  padding: 12px var(--layout-content-gutter);
  background: color-mix(in srgb, var(--color-bg-canvas) 90%, transparent);
  border-bottom: 1px solid var(--color-border-subtle);
  backdrop-filter: blur(14px);
}

.topbar-start,
.topbar-actions {
  display: flex;
  align-items: center;
}

.topbar-start {
  min-width: 0;
  gap: var(--space-3);
}

.topbar-actions {
  flex: 0 0 auto;
  gap: var(--space-2);
}

.page-context {
  min-width: 0;
}

.page-context > span {
  display: block;
  overflow: hidden;
  margin-bottom: 1px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  font-weight: 550;
  letter-spacing: 0.02em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.page-context h1 {
  overflow: hidden;
  margin: 0;
  color: var(--color-text-primary);
  font-size: 17px;
  font-weight: 650;
  letter-spacing: -0.02em;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preview-badge {
  padding: 4px 8px;
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 9px;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-pill);
}

.theme-toggle {
  display: flex;
  height: 32px;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: var(--color-surface);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  transition:
    color var(--duration-fast) var(--ease-standard),
    border-color var(--duration-fast) var(--ease-standard);
}

.theme-toggle:hover {
  color: var(--color-text-primary);
  border-color: var(--color-border-strong);
}

.theme-toggle svg {
  width: 14px;
}

.theme-toggle span {
  font-size: 11px;
  font-weight: 550;
}

.mobile-nav-toggle {
  display: none;
  width: 34px;
  height: 34px;
  padding: 8px;
  color: var(--color-text-primary);
  cursor: pointer;
  background: var(--color-surface);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
}

.content {
  width: 100%;
  max-width: var(--layout-content-max);
  margin: 0 auto;
  padding: var(--space-6) var(--layout-content-gutter) var(--space-10);
}

.sidebar-backdrop {
  display: none;
}

@media (max-width: 1080px) {
  .app-shell {
    grid-template-columns: 1fr;
  }

  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    width: min(282px, calc(100vw - 48px));
    height: 100dvh;
    box-shadow: var(--shadow-overlay);
    transform: translateX(-104%);
    transition: transform var(--duration-base) var(--ease-standard);
  }

  .sidebar.is-open {
    transform: translateX(0);
  }

  .sidebar-close,
  .mobile-nav-toggle {
    display: grid;
    place-items: center;
  }

  .sidebar-backdrop {
    position: fixed;
    z-index: 25;
    display: block;
    inset: 0;
    cursor: default;
    background: rgba(17, 19, 22, 0.4);
    border: 0;
    backdrop-filter: blur(2px);
  }
}

@media (max-width: 720px) {
  .topbar {
    min-height: 64px;
    padding: 10px 16px;
  }

  .content {
    padding: 16px 16px 32px;
  }

  .preview-badge,
  .theme-toggle span,
  .topbar-actions :deep(.el-button span) {
    display: none;
  }

  .theme-toggle {
    width: 34px;
    justify-content: center;
    padding: 0;
  }

  .topbar-actions :deep(.el-button) {
    width: 34px;
    padding: 0;
  }
}

@media (max-width: 480px) {
  .page-context > span {
    display: none;
  }

  .page-context h1 {
    max-width: 150px;
  }
}
</style>
