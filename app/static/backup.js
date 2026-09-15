(() => {
  const exportButton = $("exportBackupBtn");
  const importButton = $("importBackupBtn");
  const fileInput = $("backupFileInput");
  if (!exportButton || !importButton || !fileInput) return;

  function backupFilename() {
    return `budget-backup-${new Date().toISOString().slice(0, 10)}.json`;
  }

  function saveFile(file) {
    const url = URL.createObjectURL(file);
    const link = document.createElement("a");
    link.href = url;
    link.download = file.name;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function downloadBackup() {
    exportButton.disabled = true;
    try {
      const response = await fetch("/api/backup", { headers: requestHeaders({ Accept: "application/json" }) });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Ошибка ${response.status}`);
      }
      const backup = await response.json();
      const file = new File([JSON.stringify(backup, null, 2)], backupFilename(), { type: "application/json" });

      const telegramPlatform = window.Telegram?.WebApp?.platform || "";
      const isMobile = telegramPlatform === "ios" || telegramPlatform === "android" ||
        /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

      if (isMobile && navigator.share && navigator.canShare?.({ files: [file] })) {
        try {
          await navigator.share({ files: [file], title: "Резервная копия бюджета" });
        } catch (shareError) {
          if (shareError?.name === "AbortError") return;
          saveFile(file);
        }
      } else {
        saveFile(file);
      }
      toast("Резервная копия создана");
    } catch (error) {
      if (error?.name !== "AbortError") toast(error.message || "Не удалось создать копию");
    } finally {
      exportButton.disabled = false;
    }
  }

  async function restoreBackup(file) {
    if (file.size > 5 * 1024 * 1024) throw new Error("Файл больше 5 МБ");
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      throw new Error("Выбранный файл не является резервной копией JSON");
    }

    if (!confirm("Восстановление заменит все текущие данные бюджета содержимым резервной копии. Продолжить?")) return;
    importButton.disabled = true;
    try {
      await api("/api/backup/restore", { method: "POST", body: JSON.stringify(payload) });
      toast("Данные восстановлены");
      await loadAll();
    } finally {
      importButton.disabled = false;
    }
  }

  exportButton.addEventListener("click", downloadBackup);
  importButton.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    fileInput.value = "";
    if (!file) return;
    try {
      await restoreBackup(file);
    } catch (error) {
      toast(error.message || "Не удалось восстановить данные");
    }
  });
})();
