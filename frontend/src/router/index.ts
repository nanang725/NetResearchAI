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
            stage: "v0.2.0",
            feature: "上下文感知与意图驱动 Agent",
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
          component: () => import("@/views/ComingSoonView.vue"),
          meta: {
            title: "引用图谱",
            stage: "v0.4.0（规划中）",
            feature: "科研知识图谱与引用关系可视化",
            roadmap: "v0.3.0：大规模知识库智能检索（规划中）",
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
