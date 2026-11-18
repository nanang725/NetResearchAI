<template>
  <div class="citation-page">
    <section class="page-card toolbar">
      <div>
        <p class="eyebrow">LOCAL CITATION NETWORK</p>
        <h2>本地引用图谱</h2>
        <p class="muted">
          节点和箭头只来自当前本地论文库及可靠匹配的参考文献。
        </p>
      </div>
      <div class="toolbar-actions">
        <el-checkbox v-model="includeIsolated">显示孤立论文</el-checkbox>
        <label class="citation-filter">
          <span>最少引用</span>
          <el-input-number
            v-model="minCitations"
            :min="0"
            :max="50"
            size="small"
            controls-position="right"
          />
        </label>
        <el-button size="small" :loading="loading" @click="loadGraph">刷新</el-button>
        <el-button size="small" plain @click="graphExpanded = !graphExpanded">
          {{ graphExpanded ? "显示详情面板" : "扩大图谱" }}
        </el-button>
        <el-button
          size="small"
          type="primary"
          :loading="rebuilding"
          @click="rebuildGraph"
        >
          重建引用关系
        </el-button>
      </div>
    </section>

    <div v-if="errorMessage" class="page-card error-box">
      <span>{{ errorMessage }}</span>
      <el-button link type="primary" @click="loadGraph">重试</el-button>
    </div>

    <section v-if="graph" class="stats-grid">
      <article class="page-card stat">
        <span>本地论文</span>
        <strong>{{ graph.stats.independent_paper_count }}</strong>
      </article>
      <article class="page-card stat">
        <span>引用边</span>
        <strong>{{ graph.stats.edge_count }}</strong>
      </article>
      <article class="page-card stat">
        <span>已匹配引用</span>
        <strong>{{ graph.stats.matched_reference_count }}</strong>
      </article>
      <article class="page-card stat">
        <span>未匹配 / 歧义</span>
        <strong>
          {{ graph.stats.unmatched_reference_count }} /
          {{ graph.stats.ambiguous_reference_count }}
        </strong>
      </article>
      <article class="page-card stat">
        <span>参考文献条目</span>
        <strong>{{ graph.stats.reference_count }}</strong>
      </article>
      <article class="page-card stat">
        <span>核心论文</span>
        <strong>{{ graph.stats.foundational_paper_count }}</strong>
      </article>
    </section>

    <div v-if="graph && graph.stats.independent_paper_count < 20" class="page-card corpus-gap">
      当前有 {{ graph.stats.independent_paper_count }} 篇独立本地论文，
      距离省赛 20 篇真实语料要求还差
      {{ 20 - graph.stats.independent_paper_count }} 篇。图谱不会用测试数据补足。
    </div>

    <div v-if="graph && graph.stats.edge_count === 0" class="page-card relation-gap">
      <strong>当前没有可靠本地引用关系</strong>
      <span>
        系统中存在 {{ graph.stats.reference_count }} 条参考文献条目，但没有可以确定连接到当前本地论文的边。
        图谱不会生成推测关系；可在需要时重建引用匹配。
      </span>
    </div>

    <div class="graph-layout" :class="{ expanded: graphExpanded }">
      <section class="page-card graph-card" v-loading="loading || selectingPaper">
        <div v-if="graph?.nodes.length" class="graph-legend" aria-label="图谱图例">
          <span><i class="legend-node foundational"></i>核心论文</span>
          <span><i class="legend-node connected"></i>引用网络论文</span>
          <span><i class="legend-node isolated"></i>孤立论文</span>
          <span><i class="legend-arrow">→</i>引用方向</span>
        </div>
        <div ref="graphElement" class="graph-canvas"></div>
        <el-empty
          v-if="graph && !graph.nodes.length && !loading"
          description="当前没有可展示的本地论文引用数据"
        />
        <p v-if="graph?.nodes.length" class="graph-guide">
          拖拽节点调整位置 · 滚轮缩放 · 点击节点查看引用详情
        </p>
        <p v-if="graph" class="graph-note">{{ graph.disclaimer }}</p>
      </section>

      <aside v-if="!graphExpanded" class="page-card detail-panel">
        <template v-if="selectedPaper">
          <div class="detail-head">
            <div>
              <h3>{{ selectedPaper.title }}</h3>
              <p class="muted">
                {{ selectedPaper.year || "年份未知" }} · PageRank
                {{ selectedPaper.pagerank.toFixed(6) }}
              </p>
            </div>
            <el-button
              type="primary"
              size="small"
              @click="openPaper(selectedPaper.paper_id)"
            >
              打开论文
            </el-button>
          </div>
          <div class="detail-metrics">
            <el-tag>入度 {{ selectedPaper.in_degree }}</el-tag>
            <el-tag>出度 {{ selectedPaper.out_degree }}</el-tag>
            <el-tag>排名 {{ selectedPaper.rank }}</el-tag>
            <el-tag v-if="selectedPaper.is_foundational" type="warning">
              核心论文
            </el-tag>
          </div>
          <ul v-if="selectedPaper.foundational_reasons.length" class="reason-list">
            <li v-for="reason in selectedPaper.foundational_reasons" :key="reason">
              {{ reason }}
            </li>
          </ul>

          <h4>它引用的本地论文</h4>
          <el-empty
            v-if="!selectedPaper.outgoing.length"
            description="没有可靠匹配的本地被引论文"
            :image-size="54"
          />
          <article
            v-for="relation in selectedPaper.outgoing"
            :key="`out-${relation.reference_id}`"
            class="relation"
          >
            <div class="relation-title">
              <strong>{{ relation.title }}</strong>
              <el-button link type="primary" @click="openPaper(relation.paper_id)">
                目标论文
              </el-button>
              <el-button link type="primary" @click="openPaper(selectedPaper.paper_id, relation.source_page)">
                引用页
              </el-button>
            </div>
            <p class="muted">
              第 {{ relation.source_page }} 页 · {{ relation.match_method }} ·
              {{ (relation.match_confidence * 100).toFixed(1) }}%
            </p>
            <blockquote>{{ relation.raw_reference }}</blockquote>
          </article>

          <h4>引用它的本地论文</h4>
          <el-empty
            v-if="!selectedPaper.incoming.length"
            description="暂无本地论文引用它"
            :image-size="54"
          />
          <article
            v-for="relation in selectedPaper.incoming"
            :key="`in-${relation.reference_id}`"
            class="relation"
          >
            <div class="relation-title">
              <strong>{{ relation.title }}</strong>
              <el-button link type="primary" @click="openPaper(relation.paper_id, relation.source_page)">
                查看
              </el-button>
            </div>
            <p class="muted">
              引用位于第 {{ relation.source_page }} 页 ·
              {{ relation.match_method }}
            </p>
            <blockquote>{{ relation.raw_reference }}</blockquote>
          </article>
        </template>
        <el-empty
          v-else
          description="点击图中的论文节点查看引用详情"
        />
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import * as echarts from "echarts";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  fetchCitationGraph,
  fetchCitationPaper,
  rebuildCitations,
} from "@/api/citations";
import { ApiError } from "@/api/http";
import type {
  CitationGraphData,
  CitationPaperDetail,
} from "@/types/citation";
import {
  citationEChartsSeries,
  paperPageRoute,
} from "@/utils/citationGraph";
import { useTheme } from "@/composables/useTheme";

const router = useRouter();
const graphElement = ref<HTMLElement | null>(null);
const graph = ref<CitationGraphData | null>(null);
const selectedPaper = ref<CitationPaperDetail | null>(null);
const loading = ref(false);
const selectingPaper = ref(false);
const rebuilding = ref(false);
const includeIsolated = ref(true);
const minCitations = ref(0);
const errorMessage = ref("");
const graphExpanded = ref(false);
let chart: echarts.ECharts | null = null;
const { theme } = useTheme();

async function loadGraph() {
  loading.value = true;
  errorMessage.value = "";
  try {
    graph.value = await fetchCitationGraph({
      limit: 200,
      min_citations: minCitations.value,
      include_isolated: includeIsolated.value,
    });
    await nextTick();
    renderGraph();
  } catch (error) {
    errorMessage.value = error instanceof ApiError
      ? error.message
      : "加载引用图谱失败";
  } finally {
    loading.value = false;
  }
}

function renderGraph() {
  if (!graphElement.value || !graph.value) return;
  if (!chart) {
    chart = echarts.init(graphElement.value);
    chart.on("click", (params) => {
      if (params.dataType !== "node") return;
      const paperId = Number(
        (params.data as { paper_id?: number }).paper_id,
      );
      if (Number.isInteger(paperId)) void selectPaper(paperId);
    });
  }
  const styles = getComputedStyle(document.documentElement);
  const cssValue = (name: string) => styles.getPropertyValue(name).trim();
  const series = citationEChartsSeries(graph.value, {
    foundational: cssValue("--color-accent"),
    connected: cssValue("--color-primary"),
    isolated: cssValue("--color-text-tertiary"),
  });
  chart.setOption(
    {
      tooltip: {
        backgroundColor: cssValue("--color-surface-overlay"),
        borderColor: cssValue("--color-border-default"),
        textStyle: { color: cssValue("--color-text-primary") },
        formatter: (params: any) => {
          if (params.dataType === "edge") {
            return `引用方向：${params.data.source} → ${params.data.target}`;
          }
          const raw = params.data.raw;
          return [
            raw.title,
            `年份：${raw.year || "未知"}`,
            `入度：${raw.in_degree}，出度：${raw.out_degree}`,
            `PageRank：${raw.pagerank}`,
          ].join("<br/>");
        },
      },
      legend: { show: false },
      textStyle: { color: cssValue("--color-text-secondary") },
      animationDurationUpdate: 400,
      series: [
        {
          type: "graph",
          layout: "force",
          data: series.data,
          links: series.links,
          categories: series.categories,
          roam: true,
          draggable: true,
          edgeSymbol: ["none", "arrow"],
          edgeSymbolSize: [0, 9],
          force: { repulsion: 260, edgeLength: [90, 180], gravity: 0.08 },
          label: {
            show: true,
            position: "right",
            color: cssValue("--color-text-primary"),
            formatter: (params: any) =>
              params.data.name.length > 22
                ? `${params.data.name.slice(0, 22)}…`
                : params.data.name,
          },
          lineStyle: {
            color: cssValue("--color-border-strong"),
            width: 1.4,
            curveness: 0.08,
          },
          emphasis: { focus: "adjacency", lineStyle: { width: 3 } },
        },
      ],
    },
    true,
  );
  chart.resize();
}

async function selectPaper(paperId: number) {
  selectingPaper.value = true;
  try {
    selectedPaper.value = await fetchCitationPaper(paperId);
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "加载论文引用详情失败");
  } finally {
    selectingPaper.value = false;
  }
}

async function rebuildGraph() {
  try {
    await ElMessageBox.confirm(
      "将从现有参考文献元素重新构建引用记录和本地引用边。不会修改 PDF、页面、Chunk 或 Chroma。",
      "重建引用关系",
      { type: "warning", confirmButtonText: "开始重建", cancelButtonText: "取消" },
    );
    rebuilding.value = true;
    const result = await rebuildCitations();
    ElMessage.success(
      `重建完成：${result.reference_count} 条参考文献，${result.edge_count} 条本地引用边。`,
    );
    selectedPaper.value = null;
    await loadGraph();
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    ElMessage.error(error instanceof ApiError ? error.message : "重建引用关系失败");
  } finally {
    rebuilding.value = false;
  }
}

function openPaper(paperId: number, pageNumber = 1) {
  router.push(paperPageRoute(paperId, pageNumber));
}

function resizeChart() {
  chart?.resize();
}

watch([includeIsolated, minCitations], () => {
  void loadGraph();
});

watch(theme, async () => {
  if (!graph.value) return;
  chart?.dispose();
  chart = null;
  await nextTick();
  renderGraph();
});

onMounted(() => {
  window.addEventListener("resize", resizeChart);
  void loadGraph();
});

onUnmounted(() => {
  window.removeEventListener("resize", resizeChart);
  chart?.dispose();
  chart = null;
});
</script>

<style scoped>
.citation-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.toolbar,
.detail-head,
.relation-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.toolbar {
  padding: 18px 20px;
}

.toolbar h2 {
  font-size: 22px;
}

.toolbar h2 {
  margin: 0 0 6px;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.citation-filter {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.citation-filter :deep(.el-input-number) {
  width: 104px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(110px, 1fr));
  gap: 10px;
}

.stat {
  padding: 14px;
}

.stat span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 5px;
}

.stat strong {
  font-size: 22px;
}

.corpus-gap,
.error-box,
.relation-gap {
  padding: 12px 16px;
  color: var(--color-warning);
  background: var(--color-warning-soft);
}

.relation-gap {
  display: flex;
  align-items: center;
  gap: 16px;
  border-color: color-mix(in srgb, var(--color-warning) 28%, transparent);
}

.relation-gap strong {
  flex: 0 0 auto;
}

.relation-gap span {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.error-box {
  display: flex;
  justify-content: space-between;
  color: var(--color-danger);
  background: var(--color-danger-soft);
}

.graph-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 350px;
  gap: 16px;
}

.graph-layout.expanded {
  grid-template-columns: minmax(0, 1fr);
}

.graph-card {
  position: relative;
  min-height: 640px;
  padding: 8px;
}

.graph-layout.expanded .graph-card,
.graph-layout.expanded .graph-canvas {
  min-height: 720px;
  height: 720px;
}

.graph-canvas {
  width: 100%;
  height: 610px;
}

.graph-legend {
  position: absolute;
  top: 18px;
  left: 18px;
  z-index: 3;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 7px 9px;
  color: var(--color-text-secondary);
  background: color-mix(in srgb, var(--color-surface) 88%, transparent);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  backdrop-filter: blur(10px);
  font-size: 10px;
}

.graph-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.legend-node {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-primary);
}

.legend-node.foundational {
  background: var(--color-accent);
}

.legend-node.isolated {
  background: var(--color-text-tertiary);
}

.legend-arrow {
  color: var(--color-text-tertiary);
  font-style: normal;
}

.graph-guide {
  position: absolute;
  right: 20px;
  bottom: 36px;
  margin: 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.graph-note {
  color: var(--muted);
  font-size: 12px;
  padding: 0 12px 8px;
}

.detail-panel {
  padding: 20px;
  max-height: 720px;
  overflow-y: auto;
}

.detail-panel h3,
.detail-panel h4 {
  margin: 0 0 10px;
}

.detail-panel h4 {
  margin-top: 22px;
}

.detail-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.reason-list {
  padding-left: 18px;
  color: var(--muted);
  font-size: 12px;
}

.relation {
  border-top: 1px solid var(--line);
  padding-top: 10px;
  margin-top: 10px;
}

.relation blockquote {
  margin: 8px 0 0;
  padding: 8px 10px;
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: 12px;
  overflow-wrap: anywhere;
}

@media (max-width: 1200px) {
  .stats-grid {
    grid-template-columns: repeat(3, minmax(110px, 1fr));
  }
  .graph-layout {
    grid-template-columns: 1fr;
  }
  .detail-panel {
    max-height: none;
  }
}

@media (max-width: 720px) {
  .toolbar,
  .toolbar-actions,
  .relation-gap {
    align-items: flex-start;
    flex-direction: column;
  }

  .toolbar-actions {
    width: 100%;
  }

  .graph-legend {
    position: static;
    margin: 4px;
  }

  .graph-canvas {
    min-height: 480px;
    height: 480px;
  }

  .graph-guide {
    position: static;
    padding: 0 10px 8px;
  }
}
</style>
