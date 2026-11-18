<template>
  <div class="knowledge-page">
    <section class="page-card knowledge-hero">
      <div>
        <p class="eyebrow">V0.5.0 · SCIENTIFIC KNOWLEDGE GRAPH</p>
        <h2>科研知识图谱与跨论文知识关联</h2>
        <p class="muted">
          从当前本地论文 Chunk 中抽取作者、主题、方法、数据集、模型、发现和机构。
          每条关系都保留论文、页码、Chunk 与原始证据。
        </p>
      </div>
      <div class="hero-actions">
        <el-button :loading="loading" @click="loadGraph">刷新图谱</el-button>
        <el-button
          type="primary"
          :loading="rebuilding"
          @click="rebuildGraph"
        >
          重建知识图谱
        </el-button>
      </div>
    </section>

    <section class="page-card filter-panel">
      <el-input
        v-model="searchQuery"
        clearable
        placeholder="搜索实体，例如 RAG、Hybrid Retrieval、作者或机构"
        class="search-input"
        @keyup.enter="loadGraph"
      >
        <template #append>
          <el-button @click="loadGraph">查询</el-button>
        </template>
      </el-input>
      <el-select
        v-model="selectedNodeTypes"
        multiple
        collapse-tags
        collapse-tags-tooltip
        placeholder="实体类型"
      >
        <el-option
          v-for="item in KNOWLEDGE_NODE_TYPES"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-select
        v-model="selectedRelationTypes"
        multiple
        collapse-tags
        collapse-tags-tooltip
        placeholder="关系类型"
      >
        <el-option
          v-for="relationType in KNOWLEDGE_RELATION_TYPES"
          :key="relationType"
          :label="relationType"
          :value="relationType"
        />
      </el-select>
      <el-button plain @click="loadGraph">应用筛选</el-button>
    </section>

    <div v-if="errorMessage" class="page-card error-box">
      <span>{{ errorMessage }}</span>
      <el-button link type="primary" @click="loadGraph">重试</el-button>
    </div>

    <template v-if="graph">
      <section class="stats-grid">
        <article class="page-card stat">
          <span>知识节点</span>
          <strong>{{ graph.stats.node_count }}</strong>
        </article>
        <article class="page-card stat">
          <span>跨实体关系</span>
          <strong>{{ graph.stats.edge_count }}</strong>
        </article>
        <article class="page-card stat">
          <span>证据完整关系</span>
          <strong>{{ graph.stats.evidence_backed_edge_count }}</strong>
        </article>
        <article class="page-card stat">
          <span>本地论文</span>
          <strong>{{ graph.stats.paper_count }}</strong>
        </article>
        <article class="page-card stat">
          <span>核心论文</span>
          <strong>{{ graph.stats.core_paper_count }}</strong>
        </article>
        <article class="page-card stat">
          <span>缺失溯源</span>
          <strong>{{ graph.stats.missing_provenance_edge_count }}</strong>
        </article>
      </section>

      <el-alert
        v-if="graph.latest_rebuild?.status === 'failed'"
        :title="graph.latest_rebuild.error || '上次图谱重建失败'"
        type="error"
        :closable="false"
        show-icon
      />
      <el-alert
        v-else-if="graph.latest_rebuild?.diagnostics.length"
        :title="`重建诊断 ${graph.latest_rebuild.diagnostics.length} 条：部分数据因无法定位证据而未入图`"
        type="warning"
        :closable="false"
        show-icon
      />

      <el-tabs v-model="activeTab" class="page-card knowledge-tabs">
        <el-tab-pane label="交互图谱" name="graph">
          <div
            class="graph-layout"
            :class="{ expanded: graphExpanded }"
          >
            <section class="page-card graph-card">
              <div class="graph-toolbar">
                <span>{{ graph.nodes.length }} 个节点 · {{ graph.edges.length }} 条关系</span>
                <el-button
                  link
                  type="primary"
                  @click="graphExpanded = !graphExpanded"
                >
                  {{ graphExpanded ? "显示详情面板" : "扩大图谱" }}
                </el-button>
              </div>
              <div v-if="graph.nodes.length" ref="graphElement" class="graph-canvas" />
              <el-empty
                v-else
                description="当前没有图谱节点。请先重建知识图谱，或清理筛选条件。"
              />
              <p class="graph-note">{{ graph.disclaimer }}</p>
            </section>

            <aside class="page-card detail-panel">
              <el-select
                v-model="pickedNodeId"
                filterable
                clearable
                placeholder="选择节点查看证据"
                class="node-picker"
                @change="handleNodePick"
              >
                <el-option
                  v-for="node in graph.nodes"
                  :key="node.id"
                  :label="`${nodeTypeLabel(node.node_type)} · ${node.label}`"
                  :value="node.id"
                />
              </el-select>
              <template v-if="nodeDetail">
                <div class="detail-head">
                  <div>
                    <el-tag effect="plain">
                      {{ nodeTypeLabel(nodeDetail.node.node_type) }}
                    </el-tag>
                    <h3>{{ nodeDetail.node.label }}</h3>
                  </div>
                  <el-button
                    v-if="nodeDetail.node.paper_id"
                    plain
                    @click="openPaper(nodeDetail.node.paper_id, 1)"
                  >
                    打开论文
                  </el-button>
                </div>
                <div class="detail-metrics">
                  <el-tag size="small">度 {{ nodeDetail.node.degree }}</el-tag>
                  <el-tag size="small">
                    PageRank {{ nodeDetail.node.pagerank.toFixed(5) }}
                  </el-tag>
                  <el-tag v-if="nodeDetail.node.is_core" size="small" type="warning">
                    核心论文
                  </el-tag>
                </div>
                <h4>关系与证据</h4>
                <el-empty
                  v-if="!nodeDetail.relations.length"
                  description="该节点暂无带证据关系"
                  :image-size="52"
                />
                <article
                  v-for="relation in nodeDetail.relations"
                  :key="relation.id"
                  class="relation-card"
                >
                  <div class="relation-head">
                    <el-tag size="small" type="warning">
                      {{ relation.relation_type }}
                    </el-tag>
                    <span>{{ relation.direction === "incoming" ? "入边" : "出边" }}</span>
                  </div>
                  <strong>
                    {{ relation.source_label }}
                    →
                    {{ relation.target_label }}
                  </strong>
                  <p class="relation-source">
                    {{ relation.paper_title || `论文 ${relation.paper_id}` }}
                    · 第 {{ relation.page_number }} 页 · Chunk {{ relation.chunk_id }}
                  </p>
                  <blockquote>{{ relation.evidence_text }}</blockquote>
                  <div class="relation-actions">
                    <span>置信度 {{ relation.confidence.toFixed(2) }}</span>
                    <el-button
                      link
                      type="primary"
                      @click="openPaper(relation.paper_id, relation.page_number)"
                    >
                      查看原文
                    </el-button>
                  </div>
                </article>
              </template>
              <div v-else class="detail-placeholder">
                <p class="eyebrow">NODE INSPECTOR</p>
                <h3>选择一个图谱节点</h3>
                <p class="muted">
                  点击节点查看实体属性、全部入边/出边、论文页码、Chunk 和证据原文。
                </p>
                <div class="node-shortcuts">
                  <button
                    v-for="node in graph.nodes.slice(0, 8)"
                    :key="node.id"
                    type="button"
                    class="node-shortcut"
                    @click="selectNode(node.id)"
                  >
                    <span>{{ nodeTypeLabel(node.node_type) }}</span>
                    {{ node.label }}
                  </button>
                </div>
              </div>
            </aside>
          </div>
        </el-tab-pane>

        <el-tab-pane label="核心论文" name="core">
          <div class="analysis-header">
            <div>
              <h3>核心论文识别</h3>
              <p class="muted">
                按直接论文关系与共享科研实体投影进行加权 PageRank 排序。
              </p>
            </div>
            <el-button :loading="analysisLoading" @click="loadCorePapers">
              重新计算
            </el-button>
          </div>
          <el-empty
            v-if="!corePapers?.papers.length"
            description="重建图谱后，具有跨论文关系的论文会在此显示。"
          />
          <div v-else class="core-grid">
            <article
              v-for="paper in corePapers.papers"
              :key="paper.id"
              class="page-card core-card"
            >
              <div class="core-rank">#{{ paper.paper_rank }}</div>
              <h4>{{ paper.label }}</h4>
              <p class="muted">
                PageRank {{ (paper.paper_pagerank || 0).toFixed(5) }} ·
                入度 {{ paper.paper_in_degree || 0 }}
              </p>
              <ul>
                <li v-for="reason in paper.reasons" :key="reason">{{ reason }}</li>
              </ul>
              <el-button
                v-if="paper.paper_id"
                link
                type="primary"
                @click="openPaper(paper.paper_id, 1)"
              >
                打开论文
              </el-button>
            </article>
          </div>
        </el-tab-pane>

        <el-tab-pane label="方法比较" name="methods">
          <div class="analysis-form">
            <div>
              <h3>跨论文方法比较</h3>
              <p class="muted">只展示能从 Chunk 中定位到的 USES_METHOD 关系。</p>
            </div>
            <el-select
              v-model="methodPaperIds"
              multiple
              filterable
              :multiple-limit="5"
              placeholder="选择 2～5 篇论文"
            >
              <el-option
                v-for="paper in papers"
                :key="paper.id"
                :label="paper.title"
                :value="paper.id"
              />
            </el-select>
            <el-button
              type="primary"
              :loading="analysisLoading"
              :disabled="methodPaperIds.length < 2"
              @click="loadMethodComparison"
            >
              比较方法
            </el-button>
          </div>
          <el-empty
            v-if="!methodComparison"
            description="选择论文后比较共享方法与独有方法。"
          />
          <template v-else>
            <div class="method-summary">
              <span>
                {{ methodComparison.methods.length }} 个方法实体，
                {{ methodComparison.shared_methods.length }} 个被多篇论文使用
              </span>
            </div>
            <div class="method-grid">
              <article
                v-for="method in methodComparison.methods"
                :key="method.method_node_id"
                class="page-card method-card"
              >
                <div class="method-title">
                  <h4>{{ method.method }}</h4>
                  <el-tag :type="method.paper_ids.length > 1 ? 'success' : 'info'">
                    {{ method.paper_ids.length }} 篇论文
                  </el-tag>
                </div>
                <p class="muted">
                  {{ method.papers.map((paper) => paper.title).join("；") }}
                </p>
                <details
                  v-for="evidence in method.evidence"
                  :key="evidence.edge_id"
                >
                  <summary>
                    第 {{ evidence.page_number }} 页 · Chunk {{ evidence.chunk_id }}
                  </summary>
                  <blockquote>{{ evidence.evidence_text }}</blockquote>
                  <el-button
                    link
                    type="primary"
                    @click="openPaper(evidence.paper_id, evidence.page_number)"
                  >
                    查看原文
                  </el-button>
                </details>
              </article>
            </div>
          </template>
        </el-tab-pane>

        <el-tab-pane label="关系路径" name="path">
          <div class="analysis-form path-form">
            <div>
              <h3>论文关系路径</h3>
              <p class="muted">以无向遍历寻找最短关系链，方向仍保留在每条证据边上。</p>
            </div>
            <el-select v-model="pathSourceId" filterable placeholder="起点论文">
              <el-option
                v-for="paper in papers"
                :key="paper.id"
                :label="paper.title"
                :value="paper.id"
              />
            </el-select>
            <el-select v-model="pathTargetId" filterable placeholder="终点论文">
              <el-option
                v-for="paper in papers"
                :key="paper.id"
                :label="paper.title"
                :value="paper.id"
              />
            </el-select>
            <el-button
              type="primary"
              :loading="analysisLoading"
              :disabled="!pathSourceId || !pathTargetId || pathSourceId === pathTargetId"
              @click="loadPath"
            >
              查找路径
            </el-button>
          </div>
          <el-empty
            v-if="!pathResult"
            description="选择两篇不同论文，查看它们如何通过主题、方法或引用关联。"
          />
          <section v-else class="page-card path-result">
            <el-alert
              v-if="!pathResult.path_found"
              title="当前图谱中没有满足深度限制的关系路径。"
              type="info"
              :closable="false"
              show-icon
            />
            <template v-else>
              <div class="path-chain">
                <template v-for="(node, index) in pathResult.nodes" :key="node.id">
                  <span class="path-node" :class="node.node_type">
                    {{ node.label }}
                  </span>
                  <span v-if="index < pathResult.edges.length" class="path-edge">
                    → {{ pathResult.edges[index]?.relation_type }} →
                  </span>
                </template>
              </div>
              <p class="muted">最短路径深度：{{ pathResult.depth }}</p>
              <article
                v-for="edge in pathResult.edges"
                :key="edge.id"
                class="path-evidence"
              >
                <strong>{{ edge.source_label }} → {{ edge.target_label }}</strong>
                <span>
                  {{ edge.relation_type }} · {{ edge.paper_title }}
                  · 第 {{ edge.page_number }} 页 · Chunk {{ edge.chunk_id }}
                </span>
                <blockquote>{{ edge.evidence_text }}</blockquote>
                <el-button
                  link
                  type="primary"
                  @click="openPaper(edge.paper_id, edge.page_number)"
                >
                  查看证据原文
                </el-button>
              </article>
            </template>
          </section>
        </el-tab-pane>

        <el-tab-pane label="研究空白" name="gaps">
          <div class="analysis-form">
            <div>
              <h3>研究空白探索</h3>
              <p class="muted">
                基于本地覆盖、方法使用与冲突证据生成提示，不自动判断学术事实。
              </p>
            </div>
            <el-input
              v-model="gapTopic"
              clearable
              placeholder="可选：限定主题，例如 Information Retrieval"
              @keyup.enter="loadGaps"
            />
            <el-button type="primary" :loading="analysisLoading" @click="loadGaps">
              探索空白
            </el-button>
          </div>
          <el-empty
            v-if="!researchGaps?.gaps.length"
            description="当前筛选下没有发现可审计的覆盖缺口。"
          />
          <div v-else class="gap-grid">
            <article
              v-for="(gap, index) in researchGaps.gaps"
              :key="`${gap.gap_type}-${index}`"
              class="page-card gap-card"
            >
              <div class="gap-head">
                <el-tag type="warning">{{ gap.gap_type }}</el-tag>
                <span>{{ gap.paper_ids.length }} 篇相关论文</span>
              </div>
              <h4>{{ gap.title }}</h4>
              <p>{{ gap.description }}</p>
              <details
                v-for="evidence in gap.evidence"
                :key="evidence.edge_id"
              >
                <summary>
                  第 {{ evidence.page_number }} 页 · Chunk {{ evidence.chunk_id }}
                </summary>
                <blockquote>{{ evidence.evidence_text }}</blockquote>
                <el-button
                  link
                  type="primary"
                  @click="openPaper(evidence.paper_id, evidence.page_number)"
                >
                  查看原文
                </el-button>
              </details>
            </article>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>

    <el-empty
      v-else-if="!loading"
      description="尚未读取知识图谱。"
    />
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  ref,
  watch,
} from "vue";
import { useRouter } from "vue-router";
import * as echarts from "echarts";
import { ElMessage, ElMessageBox } from "element-plus";
import { fetchPapers } from "@/api/papers";
import {
  fetchCoreKnowledgePapers,
  fetchKnowledgeGraph,
  fetchKnowledgeMethodComparison,
  fetchKnowledgeNode,
  fetchKnowledgePath,
  fetchResearchGaps,
  rebuildKnowledgeGraph,
} from "@/api/knowledgeGraph";
import { ApiError } from "@/api/http";
import { useTheme } from "@/composables/useTheme";
import type {
  CoreKnowledgePapersData,
  KnowledgeGapsData,
  KnowledgeGraphData,
  KnowledgeMethodComparisonData,
  KnowledgeNodeDetail,
  KnowledgeNodeType,
  KnowledgePathData,
  KnowledgeRelationType,
} from "@/types/knowledgeGraph";
import type { PaperListItem } from "@/types/paper";
import {
  KNOWLEDGE_NODE_TYPES,
  KNOWLEDGE_RELATION_TYPES,
  knowledgeGraphEChartsSeries,
  nodeTypeLabel,
} from "@/utils/knowledgeGraph";
import { paperPageRoute } from "@/utils/citationGraph";

const router = useRouter();
const { theme } = useTheme();
const graphElement = ref<HTMLElement | null>(null);
const graph = ref<KnowledgeGraphData | null>(null);
const nodeDetail = ref<KnowledgeNodeDetail | null>(null);
const corePapers = ref<CoreKnowledgePapersData | null>(null);
const methodComparison = ref<KnowledgeMethodComparisonData | null>(null);
const pathResult = ref<KnowledgePathData | null>(null);
const researchGaps = ref<KnowledgeGapsData | null>(null);
const papers = ref<PaperListItem[]>([]);
const activeTab = ref("graph");
const graphExpanded = ref(false);
const loading = ref(false);
const rebuilding = ref(false);
const analysisLoading = ref(false);
const errorMessage = ref("");
const searchQuery = ref("");
const gapTopic = ref("");
const selectedNodeTypes = ref<KnowledgeNodeType[]>(
  KNOWLEDGE_NODE_TYPES.map((item) => item.value),
);
const selectedRelationTypes = ref<KnowledgeRelationType[]>([
  ...KNOWLEDGE_RELATION_TYPES,
]);
const methodPaperIds = ref<number[]>([]);
const pathSourceId = ref<number | null>(null);
const pathTargetId = ref<number | null>(null);
const pickedNodeId = ref<number | undefined>();
let chart: echarts.ECharts | null = null;

const paperNodes = computed(
  () => graph.value?.nodes.filter((node) => node.node_type === "paper") ?? [],
);

function applyPaperDefaults() {
  const ids = paperNodes.value
    .map((node) => node.paper_id)
    .filter((value): value is number => Number.isInteger(value));
  if (ids.length >= 2) {
    if (methodPaperIds.value.length < 2) methodPaperIds.value = ids.slice(0, 2);
    if (!pathSourceId.value) pathSourceId.value = ids[0];
    if (!pathTargetId.value) pathTargetId.value = ids[1];
  }
}

async function loadGraph() {
  loading.value = true;
  errorMessage.value = "";
  try {
    graph.value = await fetchKnowledgeGraph({
      node_types: selectedNodeTypes.value,
      relation_types: selectedRelationTypes.value,
      query: searchQuery.value.trim(),
      limit: 400,
    });
    applyPaperDefaults();
    if (
      nodeDetail.value
      && !graph.value.nodes.some((node) => node.id === nodeDetail.value?.node.id)
    ) {
      nodeDetail.value = null;
      pickedNodeId.value = undefined;
    }
    await nextTick();
    renderGraph();
  } catch (error) {
    errorMessage.value = error instanceof ApiError
      ? error.message
      : "科研知识图谱加载失败";
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
      const nodeId = Number((params.data as { id?: string }).id);
      if (Number.isInteger(nodeId)) void selectNode(nodeId);
    });
  }
  const styles = getComputedStyle(document.documentElement);
  const cssValue = (name: string) => styles.getPropertyValue(name).trim();
  const series = knowledgeGraphEChartsSeries(graph.value, {
    paper: cssValue("--color-primary"),
    author: "#5f8f70",
    topic: "#c9842a",
    method: "#8b6fb4",
    dataset: "#b85f6b",
    model: "#3f8f8a",
    finding: "#8d7b2f",
    institution: "#66758c",
  });
  chart.setOption(
    {
      tooltip: {
        backgroundColor: cssValue("--color-surface-overlay"),
        borderColor: cssValue("--color-border-default"),
        textStyle: { color: cssValue("--color-text-primary") },
        formatter: (params: any) => {
          if (params.dataType === "edge") {
            const edge = params.data.raw;
            return [
              `${edge.source_label} → ${edge.target_label}`,
              `关系：${edge.relation_type}`,
              `${edge.paper_title || "论文"} · 第 ${edge.page_number} 页`,
              `Chunk ${edge.chunk_id} · 置信度 ${edge.confidence}`,
            ].join("<br/>");
          }
          const node = params.data.raw;
          return [
            node.label,
            `类型：${nodeTypeLabel(node.node_type)}`,
            `度数：${node.degree} · PageRank：${node.pagerank}`,
            node.is_core ? "核心论文：是" : "",
          ].filter(Boolean).join("<br/>");
        },
      },
      legend: {
        bottom: 8,
        data: series.categories.map((item) => item.name),
        textStyle: { color: cssValue("--color-text-secondary") },
        icon: "circle",
      },
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
          edgeSymbolSize: [0, 8],
          force: {
            repulsion: 330,
            edgeLength: [95, 210],
            gravity: 0.06,
          },
          label: {
            show: true,
            position: "right",
            color: cssValue("--color-text-primary"),
            formatter: (params: any) =>
              params.data.name.length > 26
                ? `${params.data.name.slice(0, 26)}…`
                : params.data.name,
          },
          lineStyle: {
            color: cssValue("--color-border-strong"),
            width: 1.2,
            curveness: 0.08,
          },
          emphasis: {
            focus: "adjacency",
            lineStyle: { width: 3 },
          },
        },
      ],
    },
    true,
  );
  chart.resize();
}

async function selectNode(nodeId: number) {
  pickedNodeId.value = nodeId;
  try {
    nodeDetail.value = await fetchKnowledgeNode(nodeId);
  } catch (error) {
    ElMessage.error(
      error instanceof ApiError ? error.message : "节点证据读取失败",
    );
  }
}

function handleNodePick(nodeId: number | undefined) {
  if (nodeId === undefined) {
    nodeDetail.value = null;
    return;
  }
  void selectNode(nodeId);
}

async function rebuildGraph() {
  try {
    await ElMessageBox.confirm(
      "将从现有 SQLite 论文、页面、Chunk 和引用记录重建知识图谱。"
      +
      "不会修改或删除 PDF、论文、页面、Chunk、Chroma 或真实 .env。",
      "重建科研知识图谱",
      {
        type: "warning",
        confirmButtonText: "开始重建",
        cancelButtonText: "取消",
      },
    );
    rebuilding.value = true;
    const result = await rebuildKnowledgeGraph();
    ElMessage.success(
      `重建完成：${result.node_count} 个节点，`
      + `${result.edge_count} 条带证据关系。`,
    );
    nodeDetail.value = null;
    methodComparison.value = null;
    pathResult.value = null;
    researchGaps.value = null;
    corePapers.value = null;
    await loadGraph();
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    ElMessage.error(
      error instanceof ApiError ? error.message : "知识图谱重建失败",
    );
  } finally {
    rebuilding.value = false;
  }
}

async function loadCorePapers() {
  analysisLoading.value = true;
  try {
    corePapers.value = await fetchCoreKnowledgePapers();
  } catch (error) {
    ElMessage.error(
      error instanceof ApiError ? error.message : "核心论文计算失败",
    );
  } finally {
    analysisLoading.value = false;
  }
}

async function loadMethodComparison() {
  if (methodPaperIds.value.length < 2) return;
  analysisLoading.value = true;
  try {
    methodComparison.value = await fetchKnowledgeMethodComparison(
      methodPaperIds.value,
    );
  } catch (error) {
    ElMessage.error(
      error instanceof ApiError ? error.message : "方法比较失败",
    );
  } finally {
    analysisLoading.value = false;
  }
}

async function loadPath() {
  if (
    !pathSourceId.value
    || !pathTargetId.value
    || pathSourceId.value === pathTargetId.value
  ) {
    return;
  }
  analysisLoading.value = true;
  try {
    pathResult.value = await fetchKnowledgePath(
      pathSourceId.value,
      pathTargetId.value,
    );
  } catch (error) {
    ElMessage.error(
      error instanceof ApiError ? error.message : "关系路径查询失败",
    );
  } finally {
    analysisLoading.value = false;
  }
}

async function loadGaps() {
  analysisLoading.value = true;
  try {
    researchGaps.value = await fetchResearchGaps(gapTopic.value.trim());
  } catch (error) {
    ElMessage.error(
      error instanceof ApiError ? error.message : "研究空白探索失败",
    );
  } finally {
    analysisLoading.value = false;
  }
}

function openPaper(paperId: number, pageNumber = 1) {
  void router.push(paperPageRoute(paperId, pageNumber));
}

function resizeChart() {
  chart?.resize();
}

watch(activeTab, (tab) => {
  if (tab === "core") void loadCorePapers();
  if (tab === "gaps" && !researchGaps.value) void loadGaps();
});

watch(theme, async () => {
  if (!graph.value) return;
  chart?.dispose();
  chart = null;
  await nextTick();
  renderGraph();
});

onMounted(async () => {
  window.addEventListener("resize", resizeChart);
  try {
    papers.value = await fetchPapers();
  } catch (error) {
    ElMessage.error(
      error instanceof ApiError ? error.message : "论文列表加载失败",
    );
  }
  await loadGraph();
});

onUnmounted(() => {
  window.removeEventListener("resize", resizeChart);
  chart?.dispose();
  chart = null;
});
</script>

<style scoped>
.knowledge-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1480px;
  margin: 0 auto;
}

.knowledge-hero,
.filter-panel,
.analysis-form,
.analysis-header,
.graph-toolbar,
.detail-head,
.method-title,
.gap-head,
.relation-head,
.relation-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.knowledge-hero {
  padding: 20px;
}

.knowledge-hero h2 {
  margin: 4px 0 6px;
  font-size: 24px;
}

.knowledge-hero p {
  margin: 0;
}

.hero-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  gap: 8px;
}

.filter-panel {
  display: grid;
  padding: 14px;
  grid-template-columns: minmax(260px, 1.5fr) minmax(180px, 0.8fr)
    minmax(220px, 1fr) auto;
}

.search-input {
  min-width: 0;
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
  margin-bottom: 5px;
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.stat strong {
  font-family: var(--font-mono);
  font-size: 22px;
}

.error-box {
  display: flex;
  justify-content: space-between;
  color: var(--color-danger);
  background: var(--color-danger-soft);
}

.knowledge-tabs {
  padding: 16px 18px 20px;
}

.graph-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 370px;
  gap: 14px;
}

.graph-layout.expanded {
  grid-template-columns: minmax(0, 1fr);
}

.graph-card {
  position: relative;
  min-height: 680px;
  padding: 12px;
}

.graph-toolbar {
  padding: 4px 8px 8px;
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.graph-canvas {
  width: 100%;
  height: 610px;
}

.graph-note {
  padding: 0 8px;
  margin: 4px 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.detail-panel {
  min-width: 0;
  max-height: 720px;
  padding: 18px;
  overflow-y: auto;
}

.detail-head {
  align-items: flex-start;
}

.detail-head h3 {
  margin: 8px 0 0;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.detail-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 12px 0 18px;
}

.detail-panel h4 {
  margin: 0 0 10px;
}

.node-picker {
  width: 100%;
  margin-bottom: 16px;
}

.detail-placeholder {
  padding: 30px 6px;
}

.node-shortcuts {
  display: grid;
  gap: 7px;
  margin-top: 16px;
}

.node-shortcut {
  display: flex;
  min-width: 0;
  padding: 9px 10px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  text-align: left;
  gap: 8px;
}

.node-shortcut span {
  flex: 0 0 auto;
  color: var(--color-primary);
  font-size: 10px;
}

.node-shortcut:hover {
  border-color: var(--color-primary-border);
}

.relation-card,
.path-evidence {
  padding: 12px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.relation-head span,
.relation-actions span,
.relation-source {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.relation-card > strong {
  display: block;
  margin-top: 8px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.relation-source {
  margin: 5px 0;
}

blockquote {
  padding: 9px 11px;
  margin: 8px 0;
  color: var(--color-text-secondary);
  background: var(--color-bg-subtle);
  border-left: 3px solid var(--color-primary-border);
  font-size: 11px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.analysis-header,
.analysis-form {
  padding: 4px 2px 16px;
  align-items: flex-end;
}

.analysis-header h3,
.analysis-form h3 {
  margin: 0 0 4px;
}

.analysis-header p,
.analysis-form p {
  margin: 0;
}

.analysis-form :deep(.el-select),
.analysis-form :deep(.el-input) {
  min-width: 260px;
  flex: 1;
}

.path-form :deep(.el-select) {
  min-width: 190px;
}

.core-grid,
.method-grid,
.gap-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.core-card,
.method-card,
.gap-card {
  min-width: 0;
  padding: 16px;
}

.core-card {
  position: relative;
}

.core-rank {
  position: absolute;
  top: 12px;
  right: 14px;
  color: var(--color-primary);
  font-family: var(--font-mono);
  font-size: 20px;
}

.core-card h4,
.method-card h4,
.gap-card h4 {
  margin: 0 0 7px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.core-card ul {
  padding-left: 18px;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.method-summary {
  padding: 10px 12px;
  margin-bottom: 12px;
  color: var(--color-text-secondary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
  font-size: 11px;
}

.method-title,
.gap-head {
  align-items: flex-start;
}

.method-card details,
.gap-card details {
  padding-top: 8px;
  border-top: 1px solid var(--color-border-subtle);
}

.method-card summary,
.gap-card summary {
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 11px;
}

.path-result {
  padding: 18px;
}

.path-chain {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.path-node {
  max-width: 260px;
  padding: 7px 10px;
  color: var(--color-text-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-md);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.path-edge {
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 10px;
}

.path-evidence > span {
  display: block;
  margin-top: 4px;
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.gap-head > span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

@media (max-width: 1180px) {
  .stats-grid {
    grid-template-columns: repeat(3, minmax(110px, 1fr));
  }

  .filter-panel {
    grid-template-columns: 1fr 1fr;
  }

  .graph-layout {
    grid-template-columns: 1fr;
  }

  .detail-panel {
    max-height: none;
  }
}

@media (max-width: 760px) {
  .knowledge-hero,
  .analysis-header,
  .analysis-form {
    align-items: stretch;
    flex-direction: column;
  }

  .hero-actions,
  .hero-actions :deep(.el-button) {
    width: 100%;
  }

  .filter-panel,
  .core-grid,
  .method-grid,
  .gap-grid {
    grid-template-columns: 1fr;
  }

  .stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .knowledge-tabs {
    padding: 12px;
  }

  .analysis-form :deep(.el-select),
  .analysis-form :deep(.el-input) {
    width: 100%;
    min-width: 0;
  }

  .graph-canvas {
    height: 500px;
  }

  .graph-card {
    min-height: 570px;
  }
}
</style>
