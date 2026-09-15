(() => {
  const exportButton = $("exportBackupBtn");
  const importButton = $("importBackupBtn");
  const fileInput = $("backupFileInput");
  if (!exportButton || !importButton || !fileInput) return;

  async function sendBackup() {
    exportButton.disabled = true;
    try {
      await api("/api/backup/send", { method: "POST", body: "{}" });
      toast("Копия отправлена в чат с ботом");
    } catch (error) {
      toast(error.message || "Не удалось отправить копию");
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

    const missingPiggy = payload.backup_version === 1 && !Array.isArray(payload.data?.piggy_bank_movements);
    const warning = missingPiggy ? " В старой копии нет данных копилки: её текущая история и баланс будут очищены." : "";
    if (!confirm(`Восстановление заменит все текущие данные бюджета содержимым резервной копии.${warning} Продолжить?`)) return;
    importButton.disabled = true;
    try {
      await api("/api/backup/restore", { method: "POST", body: JSON.stringify(payload) });
      toast("Данные восстановлены");
      await loadAll();
    } finally {
      importButton.disabled = false;
    }
  }

  exportButton.addEventListener("click", sendBackup);
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

