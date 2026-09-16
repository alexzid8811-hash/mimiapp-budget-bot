(() => {
  const STORAGE_KEY = "budget-theme-mode";
  const MODES = new Set(["auto", "light", "dark"]);
  const tg = window.Telegram?.WebApp;
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");

  function savedMode() {
    const value = localStorage.getItem(STORAGE_KEY) || "auto";
    return MODES.has(value) ? value : "auto";
  }

  function resolvedTheme(mode) {
    if (mode === "light" || mode === "dark") return mode;
    if (tg?.colorScheme === "light" || tg?.colorScheme === "dark") return tg.colorScheme;
    return media?.matches ? "dark" : "light";
  }

  function apply(mode = savedMode(), persist = false) {
    const safeMode = MODES.has(mode) ? mode : "auto";
    const resolved = resolvedTheme(safeMode);
    document.documentElement.dataset.themeMode = safeMode;
    document.documentElement.dataset.theme = resolved;
    if (persist) localStorage.setItem(STORAGE_KEY, safeMode);

    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = resolved === "dark" ? "#10131a" : "#f4f6f8";

    const select = document.getElementById("themeMode");
    if (select && select.value !== safeMode) select.value = safeMode;
  }

  apply();

  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("themeMode");
    if (!select) return;
    select.value = savedMode();
    select.addEventListener("change", () => apply(select.value, true));
  }, { once: true });

  tg?.onEvent?.("themeChanged", () => {
    if (savedMode() === "auto") apply("auto");
  });
  media?.addEventListener?.("change", () => {
    if (savedMode() === "auto") apply("auto");
  });

  window.budgetTheme = { apply, mode: savedMode };
})();
