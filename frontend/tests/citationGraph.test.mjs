import assert from "node:assert/strict";
import test from "node:test";

import {
  buildReviewRequest,
  citationEChartsSeries,
  paperPageRoute,
  validateReviewPaperSelection,
} from "../src/utils/citationGraph.ts";


test("converts citation nodes and preserves arrow direction", () => {
  const data = {
    nodes: [
      {
        id: "1",
        paper_id: 1,
        title: "Citing",
        year: 2024,
        authors: [],
        in_degree: 0,
        out_degree: 1,
        pagerank: 0.5,
        rank: 1,
        is_foundational: false,
        foundational_reasons: [],
      },
      {
        id: "2",
        paper_id: 2,
        title: "Foundation",
        year: 2020,
        authors: [],
        in_degree: 1,
        out_degree: 0,
        pagerank: 0.5,
        rank: 2,
        is_foundational: true,
        foundational_reasons: ["test"],
      },
    ],
    edges: [
      {
        id: 9,
        source: "1",
        target: "2",
        reference_id: 3,
        raw_text: "Foundation, 2020",
        source_page: 4,
        match_method: "doi",
        match_confidence: 1,
      },
    ],
    stats: {},
    disclaimer: "",
  };
  const series = citationEChartsSeries(data);

  assert.equal(series.links[0].source, "1");
  assert.equal(series.links[0].target, "2");
  assert.equal(series.data.find((node) => node.id === "2").category, 0);
});


test("handles an empty citation graph", () => {
  const series = citationEChartsSeries({
    nodes: [],
    edges: [],
    stats: {},
    disclaimer: "",
  });
  assert.deepEqual(series.data, []);
  assert.deepEqual(series.links, []);
});


test("validates review selection and builds evidence route parameters", () => {
  assert.equal(validateReviewPaperSelection([1]), "请选择至少 2 篇论文。");
  assert.equal(validateReviewPaperSelection([1, 2, 3, 4, 5, 6]), "最多选择 5 篇论文。");
  assert.equal(validateReviewPaperSelection([1, 1]), "不能重复选择论文。");
  assert.equal(validateReviewPaperSelection([1, 2]), null);
  assert.deepEqual(buildReviewRequest([1, 2], " topic ", true), {
    paper_ids: [1, 2],
    topic: "topic",
    include_future_work: true,
  });
  assert.deepEqual(paperPageRoute(7, 3), {
    name: "paper-detail",
    params: { id: 7 },
    query: { page: "3" },
  });
});
