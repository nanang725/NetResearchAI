import { createRouter, createWebHistory } from "vue-router";
import AppLayout from "@/layouts/AppLayout.vue";
import SearchView from '../views/SearchView.vue'


const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      component: AppLayout,
      children: [
        {
          path: "",
          name: "library",
          component: () => import("@/views/LibraryView.vue"),
          meta: { title: "论文库" },
        },
        {
          path: "papers/:id",
          name: "paper-detail",
          component: () => import("@/views/PaperDetailView.vue"),
          meta: { title: "论文详情" },
        },
        {
          path: "qa",
          name: "qa",
          component: () => import("@/views/QaView.vue"),
          meta: {
            title: "智能问答",
            stage: "v0.2.0 基础能力，纳入 v0.3.0 工作台",
            feature: "上下文理解、证据问答与通用知识问答",
          },
        },
        {
          path: 'search', // 或者 '/search'
          name: 'Search',
          component: SearchView,
          meta: { title: '智能检索' }
        },  
        {
          path: "graph",
          name: "graph",
          component: () => import("@/views/CitationGraphView.vue"),
          meta: {
            title: "引用图谱",
            stage: "v0.3.0 开发预览",
            feature: "本地引用关系、PageRank 与核心论文识别；完整知识图谱规划于 v0.5.0",
          },
        },
        {
          path: "review",
          name: "review",
          component: () => import("@/views/MultiPaperReviewView.vue"),
          meta: {
            title: "多文献综述",
            stage: "v0.3.0 开发预览",
            feature: "2～5 篇本地论文的逐篇证据检索与综合综述",
          },
        },
        {
          path: "writing",
          name: "writing",
          component: () => import("@/views/AcademicWritingView.vue"),
          meta: {
            title: "学术写作与可视化",
            stage: "v0.3.0 开发预览",
            feature: "论文大纲、文献综述、References 与 CSV 数据可视化",
          },
        },
        {
          path: "discovery",
          name: "discovery",
          component: () => import("@/views/DiscoveryView.vue"),
          meta: {
            title: "文献发现",
            stage: "v0.3.0 开发预览",
            feature: "官方 arXiv 搜索、收藏入库、关键词追踪与本地/外部推荐",
          },
        },
        {
          path: "research-agent",
          name: "research-agent",
          component: () => import("@/views/ResearchAgentView.vue"),
          meta: {
            title: "自主科研 Agent",
            stage: "v0.4.0 提前预览，不属于 v0.3.0 正式目标",
            feature: "只读工具白名单、持久化计划、运行时证据校验与受控重规划",
          },
        },
        {
          path: "compare",
          name: "compare",
          component: () => import("@/views/ComingSoonView.vue"),
          meta: {
            title: "版本比较",
            stage: "尚未实现",
            feature: "论文库已有版本关系与差异查看；独立比较工作台尚未交付",
            roadmap: "v0.3.0 不将独立版本比较页面列为已完成能力，后续版本再规划。",
          },
        },
      ],
    },
  ],
});

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? "工作台";
  document.title = `${title} · NetResearch AI`;
});

export default router;
