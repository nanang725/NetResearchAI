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
          component: () => import("@/views/ComingSoonView.vue"),
          meta: {
            title: "智能问答",
            stage: "第二阶段",
            feature: "基于混合检索与 LangGraph 的论文问答",
          },
        },
        {
          path: "graph",
          name: "graph",
          component: () => import("@/views/ComingSoonView.vue"),
          meta: {
            title: "引用图谱",
            stage: "第三阶段",
            feature: "基于 NetworkX 与 ECharts 的引用关系可视化",
          },
        },
        {
          path: "compare",
          name: "compare",
          component: () => import("@/views/ComingSoonView.vue"),
          meta: {
            title: "版本比较",
            stage: "后续阶段",
            feature: "同一研究主题下论文版本与方法对比",
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
