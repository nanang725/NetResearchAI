import assert from "node:assert/strict";
import test from "node:test";
import {
  THEME_STORAGE_KEY,
  resolveThemePreference,
} from "../src/composables/useTheme.ts";


test("uses the persisted theme before the operating system preference", () => {
  assert.equal(resolveThemePreference("light", true), "light");
  assert.equal(resolveThemePreference("dark", false), "dark");
});


test("falls back to the operating system preference for missing or invalid values", () => {
  assert.equal(resolveThemePreference(null, true), "dark");
  assert.equal(resolveThemePreference(undefined, false), "light");
  assert.equal(resolveThemePreference("sepia", true), "dark");
});


test("uses a stable storage key across reloads", () => {
  assert.equal(THEME_STORAGE_KEY, "netresearch-theme");
});
