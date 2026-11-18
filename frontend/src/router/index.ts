import { createRouter, createWebHistory } from "vue-router";
import AppLayout from "@/layouts/AppLayout.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      component: AppLayout,
      children: [
        {
          path: "",
          name: "home",
          component: () => import("@/views/HomeView.vue"),
          meta: { title: "科研工作台" },
        },
        {
          path: "library",
          name: "library",
          component: () => import("@/views/LibraryView.vue"),
          meta: { title: "论文库" },
        },
        {
          path: "papers/:id",
          name: "paper-detail",
          component: () => import("@/views/PaperDetailView.vue"),
          meta: { title: "论文阅读器" },
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
          path: "search",
          name: "Search",
          component: () => import("@/views/SearchView.vue"),
          meta: { title: "智能检索" },
        },
        {
          path: "knowledge-graph",
          name: "knowledge-graph",
          component: () => import("@/views/KnowledgeGraphView.vue"),
          meta: {
            title: "科研知识图谱",
            stage: "v0.5.0 开发版本",
            feature: "科研实体、跨论文关系、PageRank、方法比较、路径与研究空白探索",
          },
        },
        {
          path: "graph",
          name: "graph",
          component: () => import("@/views/CitationGraphView.vue"),
          meta: {
            title: "引用图谱",
            stage: "v0.4.0 正式版本",
            feature: "本地引用关系、PageRank 与核心论文识别；科研实体图谱见 v0.5.0",
          },
        },
        {
          path: "review",
          name: "review",
          component: () => import("@/views/MultiPaperReviewView.vue"),
          meta: {
            title: "多文献综述",
            stage: "v0.4.0 正式版本",
            feature: "2～8 篇本地论文的逐篇证据检索与综合综述",
          },
        },
        {
          path: "writing",
          name: "writing",
          component: () => import("@/views/AcademicWritingView.vue"),
          meta: {
            title: "学术写作",
            stage: "v0.4.0 正式版本",
            feature: "论文大纲、文献综述、References 与 CSV 数据可视化",
          },
        },
        {
          path: "discovery",
          name: "discovery",
          component: () => import("@/views/DiscoveryView.vue"),
          meta: {
            title: "每日论文推荐",
            stage: "v0.4.0 正式版本",
            feature: "每日 arXiv 论文推荐、研究兴趣、收藏入库、关键词追踪与外部搜索",
          },
        },
        {
          path: "research-agent",
          name: "research-agent",
          component: () => import("@/views/ResearchAgentView.vue"),
          meta: {
            title: "自主科研 Agent",
            stage: "v0.4.0 正式版本",
            feature: "计划编辑、结构化数据流、工具编排、租约恢复与运行时证据校验",
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
            roadmap: "v0.4.0 仍不将独立版本比较页面列为已完成能力，后续版本再规划。",
          },
        },
        {
          path: ":pathMatch(.*)*",
          redirect: "/",
        },
      ],
    },
  ],
});

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? "研究工作台";
  document.title = `${title} · NetResearch AI`;
});

export default router;
