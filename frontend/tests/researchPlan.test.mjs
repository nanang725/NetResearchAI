import assert from "node:assert/strict";
import test from "node:test";
import {
  createEditableNode,
  editableNodesToPlan,
  parseNodeArguments,
  planNodesToEditable,
} from "../src/utils/researchPlan.ts";


test("converts a persisted plan into editable node fields", () => {
  const editable = planNodesToEditable({
    version: 1,
    topic: "网络流量",
    research_question: "有哪些方法？",
    time_range: null,
    analysis_dimensions: ["方法"],
    generated_by: "fallback",
    edited_by_user: false,
    nodes: [
      {
        id: "search",
        tool_name: "search_local_papers",
        arguments: { query: "流量预测", limit: 5 },
        depends_on: [],
        reason: "检索真实证据",
        completion_condition: "返回 Chunk",
        retry_limit: 1,
        failure_strategy: "retry_then_fail",
        input_bindings: {},
        output_definition: { description: "检索结果", fields: ["sources"] },
      },
    ],
  });
  assert.equal(editable.length, 1);
  assert.deepEqual(JSON.parse(editable[0].argumentsText), {
    query: "流量预测",
    limit: 5,
  });
  assert.deepEqual(editable[0].output_definition.fields, ["sources"]);
});


test("serializes edited nodes after validating ids and dependencies", () => {
  const first = createEditableNode("inspect_local_library", 1);
  first.id = "inspect";
  const second = createEditableNode("search_local_papers", 2);
  second.id = "search";
  second.depends_on = ["inspect"];
  second.argumentsText = JSON.stringify({ query: "network", limit: 3 });

  const nodes = editableNodesToPlan([first, second]);
  assert.deepEqual(nodes[1].depends_on, ["inspect"]);
  assert.deepEqual(nodes[1].arguments, { query: "network", limit: 3 });
});


test("rejects invalid JSON, duplicate ids and unknown dependencies", () => {
  const first = createEditableNode("inspect_local_library", 1);
  first.id = "inspect";
  first.argumentsText = "[1,2]";
  assert.throws(() => parseNodeArguments(first), /JSON 对象/);

  const duplicate = createEditableNode("inspect_local_library", 2);
  duplicate.id = "inspect";
  assert.throws(() => editableNodesToPlan([first, duplicate]), /不能重复/);

  first.argumentsText = "{}";
  const unknown = createEditableNode("inspect_local_library", 2);
  unknown.id = "second";
  unknown.depends_on = ["missing"];
  assert.throws(() => editableNodesToPlan([first, unknown]), /不存在/);
});


test("validates skip and fallback strategy safety before submission", () => {
  const critical = createEditableNode("inspect_local_library", 1);
  critical.id = "critical";
  critical.failure_strategy = "skip";
  critical.critical = true;
  assert.throws(() => editableNodesToPlan([critical]), /关键节点/);

  critical.failure_strategy = "fallback_tool";
  critical.critical = false;
  critical.fallback_tool = null;
  assert.throws(() => editableNodesToPlan([critical]), /替代工具/);
});
