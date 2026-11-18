import { computed, readonly, ref } from "vue";

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "netresearch-theme";

export function resolveThemePreference(
  storedTheme: string | null | undefined,
  prefersDark = false,
): Theme {
  if (storedTheme === "light" || storedTheme === "dark") {
    return storedTheme;
  }
  return prefersDark ? "dark" : "light";
}

function readStoredTheme(): string | null {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    return null;
  }
}

function prefersDarkMode(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function initialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const documentTheme = document.documentElement.dataset.theme;
  if (documentTheme === "light" || documentTheme === "dark") {
    return documentTheme;
  }
  return resolveThemePreference(readStoredTheme(), prefersDarkMode());
}

const currentTheme = ref<Theme>(initialTheme());
const isDark = computed(() => currentTheme.value === "dark");

export function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "dark" ? "#111316" : "#f7f8f8");
}

export function initTheme(): Theme {
  applyTheme(currentTheme.value);
  return currentTheme.value;
}

export function setTheme(theme: Theme, persist = true): void {
  currentTheme.value = theme;
  applyTheme(theme);
  if (!persist) return;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // The theme still applies for the current session when storage is unavailable.
  }
}

export function useTheme() {
  function toggleTheme(): void {
    setTheme(currentTheme.value === "dark" ? "light" : "dark");
  }

  return {
    theme: readonly(currentTheme),
    isDark,
    setTheme,
    toggleTheme,
  };
}
