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

  function applySafeArea() {
    const contentInset = Number(tg?.contentSafeAreaInset?.top);
    const safeInset = Number(tg?.safeAreaInset?.top);
    const reportedTop = Number.isFinite(contentInset) && contentInset > 0
      ? contentInset
      : Number.isFinite(safeInset) && safeInset > 0 ? safeInset : 0;
    const fullscreen = Boolean(tg?.isFullscreen);
    document.documentElement.classList.toggle("telegram-fullscreen", fullscreen);
    // iOS Telegram can report zero in fullscreen while its Close/menu controls
    // are still drawn above the WebView. Reserve that header only in fullscreen.
    const fallback = fullscreen && tg ? 86 : 0;
    document.documentElement.style.setProperty("--app-safe-top", `${Math.max(reportedTop, fallback)}px`);
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

  tg?.ready?.();
  tg?.expand?.();
  applySafeArea();
  setTimeout(applySafeArea, 100);
  setTimeout(applySafeArea, 500);
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
  tg?.onEvent?.("safeAreaChanged", applySafeArea);
  tg?.onEvent?.("contentSafeAreaChanged", applySafeArea);
  tg?.onEvent?.("viewportChanged", applySafeArea);
  tg?.onEvent?.("fullscreenChanged", applySafeArea);
  media?.addEventListener?.("change", () => {
    if (savedMode() === "auto") apply("auto");
  });

  window.budgetTheme = { apply, mode: savedMode };
})();
