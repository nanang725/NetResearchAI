import assert from "node:assert/strict";
import test from "node:test";

import {
  buildTrackingPayload,
  externalResultLabel,
  favoriteImportStatusLabel,
  favoriteImportStatusType,
  formatArxivDate,
} from "../src/utils/discovery.ts";


test("formats arXiv dates and distinguishes external/local results", () => {
  assert.match(formatArxivDate("2024-01-02T00:00:00Z"), /2024/);
  assert.equal(formatArxivDate(null), "日期未知");
  assert.equal(
    externalResultLabel({
      arxiv_id: "2401.00001",
      title: "Local",
      authors: [],
      summary: "",
      published: null,
      updated: null,
      primary_category: null,
      abs_url: "",
      pdf_url: "",
      origin: "arxiv_external",
      in_local_library: true,
      local_paper_id: 1,
      favorite_id: null,
    }),
    "本地已有",
  );
});


test("builds trimmed tracking conditions and clamps page size", () => {
  assert.deepEqual(
    buildTrackingPayload({
      name: " Retrieval watch ",
      query: " retrieval ",
      field: "title",
      sortBy: "submitted",
      sortOrder: "descending",
      pageSize: 999,
    }),
    {
      name: "Retrieval watch",
      query: "retrieval",
      field: "title",
      sort_by: "submitted",
      sort_order: "descending",
      page_size: 50,
    },
  );
  assert.throws(
    () =>
      buildTrackingPayload({
        name: "",
        query: "x",
        field: "all",
        sortBy: "relevance",
        sortOrder: "descending",
        pageSize: 10,
      }),
    /追踪名称/,
  );
});


test("maps ImportItem-backed favorite statuses consistently", () => {
  assert.equal(favoriteImportStatusLabel("queued"), "等待导入");
  assert.equal(favoriteImportStatusType("queued"), "warning");
  assert.equal(favoriteImportStatusLabel("running"), "导入中");
  assert.equal(favoriteImportStatusType("running"), "warning");
  assert.equal(favoriteImportStatusLabel("succeeded"), "已导入");
  assert.equal(favoriteImportStatusType("succeeded"), "success");
  assert.equal(favoriteImportStatusLabel("failed"), "入库失败");
  assert.equal(favoriteImportStatusType("failed"), "danger");
  assert.equal(favoriteImportStatusLabel("imported"), "已导入");
  assert.equal(favoriteImportStatusType("imported"), "success");
});
