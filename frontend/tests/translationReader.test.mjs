import assert from "node:assert/strict";
import test from "node:test";

import {
  buildTranslationRequest,
  orderedPageElements,
  translationScopeKey,
} from "../src/utils/translationReader.ts";


test("orders layout elements by persisted element_index and falls back to id", () => {
  const elements = [
    { id: 9, element_index: 2 },
    { id: 7, element_index: 1 },
    { id: 3, element_index: 1 },
  ];

  assert.deepEqual(
    orderedPageElements(elements).map((element) => element.id),
    [3, 7, 9],
  );
});


test("builds distinct whole-page and element translation requests", () => {
  assert.deepEqual(buildTranslationRequest(3, "en_to_zh"), {
    page_number: 3,
    direction: "en_to_zh",
  });
  assert.deepEqual(buildTranslationRequest(3, "zh_to_en", 42), {
    page_number: 3,
    direction: "zh_to_en",
    element_id: 42,
  });
  assert.notEqual(
    translationScopeKey(3),
    translationScopeKey(4),
  );
});
