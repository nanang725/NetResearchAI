import assert from "node:assert/strict";
import test from "node:test";
import {
  evidenceRoute,
  knowledgeGraphEChartsSeries,
  nodeTypeLabel,
} from "../src/utils/knowledgeGraph.ts";


test("maps scientific entities and relation evidence into ECharts data", () => {
  const graph = {
    nodes: [
      {
        id: 1,
        node_type: "paper",
        label: "Evidence Paper",
        normalized_name: "evidence paper",
        paper_id: 11,
        attributes: {},
        degree: 3,
        pagerank: 0.3,
        is_core: true,
      },
      {
        id: 2,
        node_type: "method",
        label: "Hybrid Retrieval",
        normalized_name: "hybrid retrieval",
        paper_id: null,
        attributes: {},
        degree: 2,
        pagerank: 0.2,
      },
    ],
    edges: [
      {
        id: 9,
        source: 1,
        target: 2,
        source_label: "Evidence Paper",
        target_label: "Hybrid Retrieval",
        source_type: "paper",
        target_type: "method",
        relation_type: "USES_METHOD",
        paper_id: 11,
        paper_title: "Evidence Paper",
        page_number: 4,
        chunk_id: 44,
        evidence_text: "We use Hybrid Retrieval.",
        confidence: 0.9,
        extraction_method: "method_lexicon_context_rule",
        attributes: {},
      },
    ],
    stats: {
      node_count: 2,
      edge_count: 1,
      paper_count: 1,
      node_type_counts: { paper: 1, method: 1 },
      relation_type_counts: { USES_METHOD: 1 },
      evidence_backed_edge_count: 1,
      missing_provenance_edge_count: 0,
      core_paper_count: 1,
    },
    latest_rebuild: null,
    disclaimer: "test",
  };

  const series = knowledgeGraphEChartsSeries(graph);

  assert.equal(series.data.length, 2);
  assert.equal(series.data[0].category, 0);
  assert.equal(series.data[0].itemStyle.borderWidth, 3);
  assert.equal(series.links[0].source, "1");
  assert.equal(series.links[0].relation_type, "USES_METHOD");
  assert.equal(series.categories.length, 8);
});


test("keeps entity labels and source pages addressable", () => {
  assert.equal(nodeTypeLabel("dataset"), "数据集");
  assert.equal(
    evidenceRoute({ paper_id: 7, page_number: 3 }),
    "/papers/7?page=3",
  );
});
