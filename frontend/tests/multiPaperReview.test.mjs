import assert from "node:assert/strict";
import test from "node:test";

import {
  claimKindLabel,
  claimKindType,
  comparisonCitationCount,
} from "../src/utils/multiPaperReview.ts";


test("distinguishes evidence, inference and insufficient comparison claims", () => {
  assert.equal(claimKindLabel("evidence"), "原文证据支持");
  assert.equal(claimKindLabel("inference"), "模型推断");
  assert.equal(claimKindLabel("insufficient_evidence"), "证据不足");
  assert.equal(claimKindType("evidence"), "success");
  assert.equal(claimKindType("inference"), "warning");
  assert.equal(claimKindType("insufficient_evidence"), "info");
});


test("counts only citations attached to comparison findings", () => {
  assert.equal(
    comparisonCitationCount({
      dimension: "methods",
      label: "研究方法",
      items: [
        {
          paper_id: 1,
          title: "Paper One",
          summary: "Evidence",
          claim_kind: "evidence",
          evidence_status: "covered",
          citations: [
            {
              paper_id: 1,
              title: "Paper One",
              page_number: 2,
              chunk_id: 10,
              evidence: "Evidence",
            },
          ],
        },
        {
          paper_id: 2,
          title: "Paper Two",
          summary: "Current evidence is insufficient.",
          claim_kind: "insufficient_evidence",
          evidence_status: "insufficient_evidence",
          citations: [],
        },
      ],
    }),
    1,
  );
});
