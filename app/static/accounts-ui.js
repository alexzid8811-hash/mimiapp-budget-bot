// Cards and accounts: where the money physically lies.  The home block with
// «Всего», the settings list (drag to reorder, archive), the setup wizard and
// the account field of the expense form.  All numbers come from /api/accounts;
// the budget is never computed here.
(() => {
  const $ = id => document.getElementById(id);
  const money = v => new Intl.NumberFormat("ru-RU", {
    style: "currency", currency: "RUB", minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Number(v || 0));
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
  const parseMoney = v => window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(String(v ?? "")) : Number(v) || 0;
  // «25 000» in the breakdown lines: no currency, kopecks only when there are some.
  const short = v => {
    const n = Number(v || 0);
    const digits = Number.isInteger(Math.round(n * 100) / 100) ? 0 : 2;
    return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
  };
  const fmtDay = iso => (iso ? new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date(`${iso}T12:00:00`)) : "");
  const fmtInput = v => (window.ruMoneyInput?.formatMoney ? window.ruMoneyInput.formatMoney(String(v)) : String(v));
  const notify = message => { if (typeof toast === "function") toast(message); };
  const show = (id, visible) => $(id)?.classList.toggle("hidden", !visible);
  const reload = () => (typeof loadAll === "function" ? loadAll() : null);

  async function request(path, options = {}) {
    if (typeof api === "function") return api(path, options);
    const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
    return res.json();
  }

  const KIND_LABELS = { debit: "Дебетовая карта", savings: "Накопительный счёт", cash: "Наличные", credit: "Кредитная карта" };
  const KIND_EMOJI = { debit: "💳", savings: "🏦", cash: "💵", credit: "💳" };

  let snap = null;
  const accounts = () => snap?.accounts || [];
  const active = () => accounts().filter(a => !a.archived);
  const archived = () => accounts().filter(a => a.archived);
  const byId = id => accounts().find(a => a.id === Number(id));
  const enabled = () => Boolean(snap?.enabled);
  const accountName = a => `${esc(a.emoji)} ${esc(a.title)}`;

  // ---------- Home ----------

  function creditLine(a) {
    const available = a.available === null || a.available === undefined ? "" : ` · доступно ${money(a.available)}`;
    return `долг ${money(a.debt)}${available}`;
  }

  function renderHome() {
    const card = $("homeAccounts");
    if (!card) return;
    card.classList.toggle("hidden", !enabled());
    if (!enabled()) return;
    $("accTotal").textContent = money(snap.total);
    const regular = active().filter(a => a.kind !== "credit");
    const credit = active().filter(a => a.kind === "credit");
    $("accList").innerHTML = regular.map(a => `
      <div class="acc-item">
        <button class="acc-row" type="button" data-account-id="${a.id}">
          <span class="acc-name">${accountName(a)}${a.is_main ? ' <i class="acc-main" title="Основной счёт">⭐</i>' : ""}</span>
          <b class="num">${money(a.balance)}</b>
        </button>${breakdownLines(a)}
      </div>`).join("") || `<div class="empty">Добавьте карты и счета в настройках.</div>`;
    $("accCredit").innerHTML = credit.map(a => `
      <button class="acc-row credit" type="button" data-account-id="${a.id}">
        <span class="acc-name">${accountName(a)}</span>
        <small class="num">${creditLine(a)}</small>
      </button>`).join("");
    show("accCredit", credit.length > 0);
  }

  // How a card's money divides between the envelopes lying on it, and what is
  // outside the budget there or missing.
  function breakdownLines(a) {
    const part = a.breakdown;
    if (!part) return "";
    const lines = [];
    if (part.envelopes.length) {
      lines.push(`<div class="acc-split num">${part.envelopes.map(e => `${esc(e.label)} ${short(e.amount)}`).join(" · ")}</div>`);
    }
    const diff = Number(part.difference) || 0;
    if (diff >= 0.005) {
      const target = ["piggy", "reserve"].find(key => a.envelopes.includes(key));
      const label = target === "piggy" ? "Добавить в копилку" : "Добавить в резерв";
      const add = target ? `<button class="tiny" type="button" data-add-to="${target}" data-amount="${diff}">${label}</button>` : "";
      lines.push(`<div class="acc-diff plus"><span>вне бюджета ${money(diff)}</span>${add}</div>`);
    } else if (diff <= -0.005) {
      lines.push(`<div class="acc-diff minus"><span>не хватает ${money(-diff)}</span><button class="tiny" type="button" data-transfer-to="${a.id}" data-amount="${-diff}">Перевести</button><button class="tiny" type="button" data-adjust-account="${a.id}">Исправить остаток</button></div>`);
    }
    return lines.join("");
  }

  async function addToEnvelope(key, value) {
    try {
      if (key === "piggy") {
        await request("/api/piggy-bank/deposit", { method: "POST", body: JSON.stringify({
          amount: value, source: "external", account_move: false, note: "Было на карте вне бюджета",
        }) });
      } else {
        await request("/api/irregular/reserve/deposit", { method: "POST", body: JSON.stringify({
          amount: value, kind: "deposit", account_move: false, reason: "Было на карте вне бюджета",
        }) });
      }
      notify(key === "piggy" ? "Добавлено в копилку" : "Добавлено в резерв");
      await reload();
    } catch (error) { notify(error.message); }
  }

  // «Не хватает»: the money most likely comes from a card with money outside the budget.
  function transferTo(id, value) {
    const donors = active().filter(a => a.kind !== "credit" && a.id !== Number(id))
      .sort((x, y) => (Number(y.breakdown?.difference) || 0) - (Number(x.breakdown?.difference) || 0));
    openTransfer({ from: donors[0]?.id ?? null, to: Number(id), amount: value });
  }

  // ---------- Settings ----------

  function settingsRow(a) {
    const value = a.kind === "credit" ? creditLine(a) : money(a.balance);
    return `
    <div class="category-order-item account-item" data-account-id="${a.id}">
      <button class="category-order-name" type="button" data-edit-account="${a.id}" aria-label="Изменить ${esc(a.title)}"><b>${esc(a.emoji)}</b><span>${esc(a.title)}${a.is_main ? " ⭐" : ""}<small class="account-sub">${esc(KIND_LABELS[a.kind] || a.kind)} · ${value}</small></span><i aria-hidden="true">✎</i></button>
      <button class="category-drag-handle" type="button" aria-label="Перетащить ${esc(a.title)}" title="Зажмите и перетащите">☰</button>
    </div>`;
  }

  function archiveRow(a) {
    const value = a.kind === "credit" ? creditLine(a) : money(a.balance);
    const nonZero = Math.abs(Number(a.balance) || 0) >= 0.005;
    const warn = nonZero
      ? `<div class="warning">${a.kind === "credit" ? "По архивной кредитке есть долг" : "На архивном счёте не ноль"}: ${value}. Верните счёт или исправьте остаток. <button class="tiny" type="button" data-adjust-account="${a.id}">Исправить остаток</button></div>` : "";
    const remove = a.has_operations ? "" : `<button class="tiny danger" type="button" data-delete-account="${a.id}">Удалить навсегда</button>`;
    return `<div class="list-row account-archived"><div class="row-main"><div class="emoji">${esc(a.emoji)}</div><div class="row-text"><div class="row-title">${esc(a.title)} <span class="pill">в архиве</span></div><div class="row-sub">${esc(KIND_LABELS[a.kind] || a.kind)} · ${value}</div>${warn}</div></div><div class="actions"><button class="tiny" type="button" data-restore-account="${a.id}">Вернуть</button>${remove}</div></div>`;
  }

  function renderSettings() {
    const toggle = $("accountsEnabled");
    if (toggle) toggle.checked = enabled();
    show("accountsSettingsBody", enabled());
    show("addAccountBtn", enabled());
    const intro = $("accountsIntro");
    if (intro) {
      intro.textContent = enabled()
        ? "Сумма всех карт, счетов и наличных — «Всего» на главной. Кредитки считаются отдельно: долг и доступный лимит."
        : accounts().length
          ? "Учёт выключен: карты, счета и их история сохранены. Включите, чтобы снова видеть остатки."
          : "Показывает, на каких картах и счетах лежат деньги и сколько их всего. Бюджет и «Можно сегодня» считаются как прежде. Чтобы включить учёт, сначала добавьте карты и счета с суммами на них.";
    }
    const list = $("accountsList");
    if (list) list.innerHTML = active().map(settingsRow).join("");
    const places = $("envelopePlaces");
    show("envelopePlacesBlock", enabled());
    if (places) {
      const regular = active().filter(a => a.kind !== "credit");
      places.innerHTML = enabled() ? (snap.envelopes || []).map(e => `
        <label>${esc(e.label)}${e.amount !== null && e.amount !== undefined ? ` · ${money(e.amount)}` : ""}<select id="envelopePlace-${e.key}" data-envelope="${e.key}">${regular.map(a => `<option value="${a.id}"${a.id === e.account_id ? " selected" : ""}>${accountName(a)}</option>`).join("")}</select></label>`).join("") : "";
    }
    const box = $("accountsArchive");
    if (box) {
      const items = archived();
      const warn = items.some(a => Math.abs(Number(a.balance) || 0) >= 0.005);
      box.innerHTML = items.length
        ? `<details class="accounts-archive"${warn ? " open" : ""}><summary>Архив (${items.length})${warn ? " ⚠️" : ""}</summary><div class="list">${items.map(archiveRow).join("")}</div></details>`
        : "";
    }
  }

  // ---------- Account dialog ----------

  function syncKindFields() {
    const kind = $("accountKind").value;
    const editing = Boolean($("accountId").value);
    show("accountCreditFields", kind === "credit");
    show("accountBalanceField", !editing);
    $("accountBalanceLabel").textContent = kind === "credit" ? "Долг сейчас" : "Сколько сейчас на счёте";
    const account = editing ? byId($("accountId").value) : null;
    show("accountMainField", Boolean(account) && kind !== "credit");
  }

  function openAccount(account = null) {
    if (!$("accountDialog")) return notify("Обновите приложение");
    $("accountDialogTitle").textContent = account ? account.title : "Новая карта или счёт";
    $("accountId").value = account?.id || "";
    $("accountEmoji").value = account?.emoji || "💳";
    $("accountTitle").value = account?.title || "";
    $("accountKind").value = account?.kind || "debit";
    $("accountKind").disabled = Boolean(account);
    $("accountBalance").value = "";
    $("accountLimit").value = account?.credit_limit ?? "";
    $("accountPaymentDay").value = account?.payment_day ?? "";
    $("accountIsMain").checked = Boolean(account?.is_main);
    $("accountIsMain").disabled = Boolean(account?.is_main);
    $("accountDialogNote").textContent = account
      ? (account.kind === "credit" ? `Долг ${money(account.debt)}. Учёт ведётся с ${fmtDay(account.opening_date)}.` : `На счёте ${money(account.balance)}. Учёт ведётся с ${fmtDay(account.opening_date)}.`)
      : "Учёт по счёту начнётся с сегодняшнего дня.";
    show("accountDangerBlock", Boolean(account));
    const del = $("deleteAccountBtn");
    if (del) del.textContent = account?.has_operations ? "Удалить (уйдёт в архив)" : "Удалить";
    syncKindFields();
    $("accountDialog").showModal();
  }

  function accountPayload() {
    const kind = $("accountKind").value;
    const day = $("accountPaymentDay").value;
    const limit = $("accountLimit").value;
    return {
      title: $("accountTitle").value.trim(),
      emoji: $("accountEmoji").value.trim() || KIND_EMOJI[kind] || "💳",
      credit_limit: kind === "credit" && String(limit).trim() !== "" ? parseMoney(limit) : null,
      payment_day: kind === "credit" && day ? Number(day) : null,
    };
  }

  async function saveAccount(event) {
    event?.preventDefault?.();
    const id = $("accountId").value;
    const kind = $("accountKind").value;
    const body = id
      ? { ...accountPayload(), is_main: $("accountIsMain").checked }
      : { ...accountPayload(), kind, balance: parseMoney($("accountBalance").value) };
    try {
      await request(id ? `/api/accounts/${id}` : "/api/accounts", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
      $("accountDialog").close();
      notify(id ? "Счёт сохранён" : "Счёт добавлен");
      await reload();
    } catch (error) { notify(error.message); }
  }

  async function archiveAccount(id) {
    try {
      await request(`/api/accounts/${id}/archive`, { method: "POST" });
      $("accountDialog")?.close();
      notify("Счёт в архиве");
      await reload();
    } catch (error) {
      const account = byId(id);
      // Money left on the account: offer to move it first.
      if (account && account.kind !== "credit" && /остаток/.test(error.message)
          && confirm(`${error.message}. Перевести ${money(account.balance)} на другой счёт?`)) {
        $("accountDialog")?.close();
        return openTransfer({ from: account.id, amount: account.balance });
      }
      notify(error.message);
    }
  }

  async function deleteAccount(id) {
    const account = byId(id);
    if (!account) return;
    const question = account.has_operations
      ? `По счёту «${account.title}» были операции: он уйдёт в архив, история сохранится. Продолжить?`
      : `Удалить «${account.title}» навсегда?`;
    if (!confirm(question)) return;
    try {
      const result = await request(`/api/accounts/${id}`, { method: "DELETE" });
      $("accountDialog")?.close();
      notify(result?.outcome === "archived" ? "Счёт в архиве: по нему были операции" : "Счёт удалён");
      await reload();
    } catch (error) { notify(error.message); }
  }

  async function restoreAccount(id) {
    try {
      await request(`/api/accounts/${id}/restore`, { method: "POST" });
      notify("Счёт снова активен");
      await reload();
    } catch (error) { notify(error.message); }
  }

  // ---------- Switch and wizard ----------

  async function toggleFeature() {
    const toggle = $("accountsEnabled");
    const wanted = Boolean(toggle?.checked);
    if (wanted === enabled()) return;
    if (wanted) {
      const ready = active().some(a => a.kind !== "credit" && a.is_main);
      if (!ready) {
        if (toggle) toggle.checked = false;
        return openWizard();
      }
    } else if (!confirm("Скрыть карты и счета? Ничего не удаляется: при включении всё вернётся.")) {
      if (toggle) toggle.checked = true;
      return;
    }
    try {
      const result = await request("/api/accounts/settings", { method: "PUT", body: JSON.stringify({ enabled: wanted }) });
      const pending = Number(result?.operations_without_account || 0);
      if (wanted && pending > 0) {
        alert(`Пока учёт был выключен, записано операций без карты: ${pending}. Они не изменили остатки. Сверьте остатки с банком и при необходимости исправьте их.`);
      } else {
        notify(wanted ? "Учёт по картам и счетам включён" : "Учёт по картам и счетам выключен");
      }
      await reload();
    } catch (error) {
      if (toggle) toggle.checked = enabled();
      notify(error.message);
    }
  }

  const wizard = { step: 1, rows: [], main: 0, envelopes: {}, info: null, balances: {} };

  function blankRow(index) {
    return index === 0
      ? { emoji: "💳", title: "Основная карта", kind: "debit", credit_limit: "", payment_day: "" }
      : { emoji: "💳", title: "", kind: "debit", credit_limit: "", payment_day: "" };
  }

  async function openWizard() {
    if (!$("accountsWizard")) return notify("Обновите приложение");
    wizard.step = 1;
    wizard.rows = [blankRow(0)];
    wizard.main = 0;
    wizard.envelopes = {};
    wizard.balances = {};
    wizard.info = null;
    try { wizard.info = await request("/api/accounts?with_amounts=true"); } catch (_) { wizard.info = null; }
    renderWizard();
    $("accountsWizard").showModal();
  }

  const regularIndexes = () => wizard.rows.map((r, i) => (r.kind === "credit" ? -1 : i)).filter(i => i >= 0);

  function readWizard() {
    if (wizard.step === 1) {
      wizard.rows = wizard.rows.map((row, i) => ({
        emoji: $(`wizEmoji-${i}`)?.value?.trim() || row.emoji,
        title: $(`wizTitle-${i}`)?.value?.trim() ?? row.title,
        kind: $(`wizKind-${i}`)?.value || row.kind,
        credit_limit: $(`wizLimit-${i}`)?.value ?? row.credit_limit,
        payment_day: $(`wizDay-${i}`)?.value ?? row.payment_day,
      }));
    } else if (wizard.step === 2) {
      wizard.main = Number($("wizMain")?.value ?? wizard.main);
      (wizard.info?.envelopes || []).forEach(e => {
        const value = $(`wizEnvelope-${e.key}`)?.value;
        if (value !== undefined && value !== "") wizard.envelopes[e.key] = Number(value);
      });
    } else {
      wizard.rows.forEach((_, i) => { wizard.balances[i] = $(`wizBalance-${i}`)?.value ?? wizard.balances[i]; });
    }
  }

  function envelopeIndex(key) {
    const value = wizard.envelopes[key];
    return value === undefined || wizard.rows[value]?.kind === "credit" ? wizard.main : value;
  }

  // Sum of the budget envelopes lying on an account: its suggested balance.
  function prefill(index) {
    if (!wizard.info?.budget_configured) return "";
    const sum = (wizard.info.envelopes || [])
      .filter(e => envelopeIndex(e.key) === index)
      .reduce((total, e) => total + Math.round(Number(e.amount || 0) * 100), 0);
    return sum ? fmtInput(sum / 100) : "";
  }

  function renderWizard() {
    const body = $("accountsWizardBody");
    if (!body) return;
    const step = wizard.step;
    $("accountsWizardStep").textContent = `Шаг ${step} из 3`;
    $("accountsWizardBack").classList.toggle("hidden", step === 1);
    $("accountsWizardNext").textContent = step === 3 ? "Включить учёт" : "Далее";
    if (step === 1) {
      $("accountsWizardTitle").textContent = "Ваши карты и счета";
      body.innerHTML = `<p class="muted">Добавьте все карты, счета, наличные и кредитки, где лежат ваши деньги.</p>${wizard.rows.map((row, i) => `
        <div class="wizard-account">
          <div class="category-fields">
            <label class="category-emoji-field">Эмодзи<input id="wizEmoji-${i}" maxlength="16" value="${esc(row.emoji)}" /></label>
            <label>Название<input id="wizTitle-${i}" maxlength="40" value="${esc(row.title)}" placeholder="Например, Сбер" /></label>
          </div>
          <label>Тип<select id="wizKind-${i}" data-wiz-kind="${i}">${Object.entries(KIND_LABELS).map(([k, v]) => `<option value="${k}"${row.kind === k ? " selected" : ""}>${v}</option>`).join("")}</select></label>
          ${row.kind === "credit" ? `<div class="grid2"><label>Кредитный лимит<input id="wizLimit-${i}" data-money inputmode="decimal" value="${esc(row.credit_limit)}" placeholder="0,00" /></label><label>День платежа<input id="wizDay-${i}" type="number" min="1" max="31" value="${esc(row.payment_day)}" placeholder="необязательно" /></label></div>` : ""}
          ${wizard.rows.length > 1 ? `<button class="tiny danger" type="button" data-wiz-remove="${i}">Убрать</button>` : ""}
        </div>`).join("")}
        <button class="secondary wide" type="button" id="wizAddRow">＋ Ещё карта или счёт</button>`;
    } else if (step === 2) {
      $("accountsWizardTitle").textContent = "Куда приходит доход";
      const options = selected => regularIndexes()
        .map(i => `<option value="${i}"${i === selected ? " selected" : ""}>${esc(wizard.rows[i].emoji)} ${esc(wizard.rows[i].title)}</option>`).join("");
      const envelopes = wizard.info?.envelopes || [];
      body.innerHTML = `
        <label>Основной счёт: сюда приходит доход<select id="wizMain">${options(wizard.main)}</select></label>
        <h4>Где лежат деньги бюджета</h4>
        <p class="muted">По умолчанию всё на основном счёте. Несколько конвертов могут лежать на одной карте.</p>
        ${envelopes.map(e => `<label>${esc(e.label)}${e.amount !== null && e.amount !== undefined ? ` · ${money(e.amount)}` : ""}<select id="wizEnvelope-${e.key}">${options(envelopeIndex(e.key))}</select></label>`).join("")}`;
    } else {
      $("accountsWizardTitle").textContent = "Сколько денег сейчас";
      body.innerHTML = `<p class="muted">Для счёта с конвертами сумма подставлена из бюджета. Исправьте её, если в банке другая сумма.</p>${wizard.rows.map((row, i) => {
        const credit = row.kind === "credit";
        const value = wizard.balances[i] ?? (credit ? fmtInput(0) : prefill(i));
        return `<label>${esc(row.emoji)} ${esc(row.title)} · ${credit ? "долг сейчас" : "сейчас на счёте"}<input id="wizBalance-${i}" data-money inputmode="decimal" value="${esc(value)}" placeholder="0,00" /></label>`;
      }).join("")}`;
    }
  }

  function validateStep() {
    if (wizard.step === 1) {
      if (wizard.rows.some(r => !r.title)) return "Укажите название каждой карты и счёта";
      if (!regularIndexes().length) return "Нужна хотя бы одна карта, счёт или наличные, куда приходит доход";
      if (wizard.rows[wizard.main]?.kind === "credit" || !wizard.rows[wizard.main]) wizard.main = regularIndexes()[0];
    }
    return "";
  }

  async function wizardNext() {
    readWizard();
    const problem = validateStep();
    if (problem) return notify(problem);
    if (wizard.step < 3) {
      wizard.step += 1;
      return renderWizard();
    }
    const payload = {
      accounts: wizard.rows.map((row, i) => ({
        title: row.title, emoji: row.emoji || KIND_EMOJI[row.kind], kind: row.kind,
        balance: parseMoney(wizard.balances[i] || 0),
        credit_limit: row.kind === "credit" && String(row.credit_limit).trim() !== "" ? parseMoney(row.credit_limit) : null,
        payment_day: row.kind === "credit" && row.payment_day ? Number(row.payment_day) : null,
        is_main: i === wizard.main,
      })),
      envelopes: Object.fromEntries((wizard.info?.envelopes || []).map(e => [e.key, envelopeIndex(e.key)])),
    };
    try {
      await request("/api/accounts/setup", { method: "POST", body: JSON.stringify(payload) });
      $("accountsWizard").close();
      notify("Учёт по картам и счетам включён");
      await reload();
    } catch (error) { notify(error.message); }
  }

  function wizardBack() {
    readWizard();
    if (wizard.step > 1) wizard.step -= 1;
    renderWizard();
  }

  function wizardClick(event) {
    if (event.target?.id === "wizAddRow") {
      readWizard();
      wizard.rows.push(blankRow(wizard.rows.length));
      return renderWizard();
    }
    const remove = event.target?.closest?.("[data-wiz-remove]");
    if (remove) {
      readWizard();
      const index = Number(remove.dataset.wizRemove);
      wizard.rows.splice(index, 1);
      wizard.envelopes = {};
      if (wizard.main >= wizard.rows.length || wizard.main === index) wizard.main = 0;
      renderWizard();
    }
  }

  function wizardChange(event) {
    if (event.target?.dataset?.wizKind !== undefined) {
      readWizard();
      renderWizard();
    }
  }

  // ---------- Account fields of the operation forms ----------
  // Every form has <prefix>AccountField, <prefix>Account, optionally
  // <prefix>AccountHint and a date input <prefix>Date.

  function envelopeAccountId(key) {
    const envelope = (snap?.envelopes || []).find(e => e.key === key);
    return envelope?.account_id ?? snap?.main_account_id ?? null;
  }

  function updateHint(prefix) {
    const hint = $(`${prefix}AccountHint`);
    if (!hint) return;
    const account = byId($(`${prefix}Account`)?.value);
    const day = $(`${prefix}Date`)?.value;
    const early = Boolean(account && day && day < account.opening_date);
    hint.textContent = early
      ? `Дата раньше начала учёта по «${account.title}» (${fmtDay(account.opening_date)}): остаток счёта эта операция не изменит.` : "";
    hint.classList.toggle("hidden", !early);
  }

  // ``transaction`` (an edit) keeps its account, even an archived one; a new
  // operation gets ``fallback``.  Credit cards only where ``credit`` allows.
  function prepareField(prefix, { transaction = null, fallback = null, credit = false, visible = true } = {}) {
    const field = $(`${prefix}AccountField`);
    const select = $(`${prefix}Account`);
    if (!field || !select) return;
    const on = enabled() && visible;
    field.classList.toggle("hidden", !on);
    if (!on) return;
    const current = transaction ? transaction.account_id : undefined;
    const options = active().filter(a => credit || a.kind !== "credit" || a.id === current)
      .map(a => `<option value="${a.id}">${accountName(a)}</option>`);
    const old = current ? byId(current) : null;
    if (old && old.archived) options.push(`<option value="${old.id}">${accountName(old)} (архив)</option>`);
    if (transaction && !current) options.push('<option value="">Без карты</option>');
    select.innerHTML = options.join("");
    select.value = String(transaction ? (current ?? "") : (fallback ?? ""));
    updateHint(prefix);
  }

  function fieldPayload(prefix) {
    if (!enabled() || $(`${prefix}AccountField`)?.classList.contains("hidden")) return {};
    const value = $(`${prefix}Account`)?.value;
    return { account_id: value ? Number(value) : null };
  }

  // «Из чего»: the daily budget always; the piggy bank or the reserve when it
  // lies on the chosen card; «Вне бюджета» always.
  const FUNDING_LABELS = { spending: "Из бюджета на траты", piggy: "Из копилки", reserve: "Из резерва", none: "Вне бюджета" };

  function fundingChoice(accountId) {
    const here = (snap?.envelopes || []).filter(e => e.account_id === Number(accountId)).map(e => e.key);
    const options = ["spending", ...["piggy", "reserve"].filter(k => here.includes(k)), "none"];
    const fallback = here.includes("spending") ? "spending"
      : here.includes("piggy") && here.includes("reserve") ? ""
        : here.includes("piggy") ? "piggy" : here.includes("reserve") ? "reserve" : "none";
    return { options, fallback };
  }

  function fillFunding(current) {
    const field = $("expenseFundingField");
    const select = $("expenseFunding");
    if (!field || !select) return;
    const account = byId($("expenseAccount")?.value);
    const on = enabled() && !$("expenseAccountField")?.classList.contains("hidden") && account && account.kind !== "credit";
    field.classList.toggle("hidden", !on);
    if (!on) return;
    const { options, fallback } = fundingChoice(account.id);
    const value = current && options.includes(current) ? current : fallback;
    select.innerHTML = (value === "" ? '<option value="">Выберите…</option>' : "")
      + options.map(k => `<option value="${k}">${FUNDING_LABELS[k]}</option>`).join("");
    select.value = value;
  }

  function prepareExpense(transaction = null) {
    prepareField("expense", { transaction, fallback: envelopeAccountId("spending") });
    const current = transaction ? (transaction.funding_envelope || "spending") : undefined;
    fillFunding(current);
  }

  function expensePayload() {
    const payload = fieldPayload("expense");
    if (!("account_id" in payload)) return payload;
    if (!$("expenseFundingField")?.classList.contains("hidden")) {
      const funding = $("expenseFunding")?.value;
      if (!funding) throw new Error("Выберите, из чего оплачена трата");
      payload.funding_envelope = funding;
    }
    return payload;
  }

  // Piggy bank and reserve: where outside money came from or went to.
  function prepareMove(prefix, { label, outside, fallback = "", visible = true }) {
    const field = $(`${prefix}AccountField`);
    const select = $(`${prefix}Account`);
    if (!field || !select) return;
    const on = enabled() && visible;
    field.classList.toggle("hidden", !on);
    if (!on) return;
    $(`${prefix}AccountLabel`).textContent = label;
    const cards = active().filter(a => a.kind !== "credit").map(a => `<option value="${a.id}">${accountName(a)}</option>`);
    select.innerHTML = (outside ? `<option value="">${outside}</option>` : "") + cards.join("");
    select.value = String(fallback ?? "");
  }

  function preparePiggy(mode) {
    const deposit = mode === "deposit";
    prepareMove("piggy", {
      label: deposit ? "Откуда" : "Куда", visible: deposit || mode === "withdraw",
      outside: deposit ? "Извне — не с моих карт" : "Наружу — потратил или отдал",
      fallback: deposit ? "" : (envelopeAccountId("spending") ?? ""),
    });
  }

  function prepareReserve(mode, purpose) {
    const deposit = mode === "deposit";
    const expense = mode === "withdraw" && purpose === "pay_expense";
    prepareMove("irrReserve", deposit
      ? { label: "Откуда", outside: "Извне — не с моих карт", fallback: "" }
      : { label: "С какой карты", outside: "", fallback: envelopeAccountId("reserve") ?? "", visible: expense });
  }
  // Income arrives on the main account by default.
  const prepareIncome = (prefix, transaction = null) =>
    prepareField(prefix, { transaction, fallback: snap?.main_account_id ?? null });

  // ---------- Account history, transfers, corrections ----------

  let historyAccount = null;

  function historyRow(item) {
    const negative = Number(item.amount) < 0;
    const amountText = `${negative ? "−" : "+"}${money(Math.abs(Number(item.amount)))}`;
    const date = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(`${item.date}T12:00:00`));
    const sub = [date, item.sub, item.counted ? "" : "до начала учёта, не в остатке"].filter(Boolean).map(esc).join(" · ");
    const remove = item.deletable
      ? `<div class="actions"><button class="tiny danger" type="button" data-history-delete="${item.type}:${item.id}">Удалить</button></div>` : "";
    return `<div class="list-row${item.counted ? "" : " not-counted"}"><div class="row-main"><div class="emoji">${esc(item.emoji)}</div><div class="row-text"><div class="row-title">${esc(item.title)}</div><div class="row-sub">${sub}</div></div></div><div><div class="amount ${negative ? "expense" : "income"}">${amountText}</div>${remove}</div></div>`;
  }

  async function openHistory(id) {
    const account = byId(id);
    if (!account || !$("accountHistoryDialog")) return;
    historyAccount = account;
    let data = null;
    try { data = await request(`/api/accounts/${id}/history`); } catch (error) { return notify(error.message); }
    const state = data?.account || account;
    historyAccount = state;
    const credit = state.kind === "credit";
    $("accountHistoryTitle").textContent = `${state.emoji} ${state.title}${state.archived ? " (архив)" : ""}`;
    $("accountHistoryCaption").textContent = credit ? "Долг" : "Остаток";
    $("accountHistoryBalance").textContent = money(credit ? state.debt : state.balance);
    $("accountHistoryNote").textContent = credit && state.available !== null && state.available !== undefined
      ? `Доступно ${money(state.available)} · учёт с ${fmtDay(state.opening_date)}` : `Учёт с ${fmtDay(state.opening_date)}`;
    const actions = [];
    if (!state.archived && !credit) {
      actions.push('<button class="secondary" type="button" data-history-action="transfer">Перевести</button>');
      actions.push('<button class="secondary" type="button" data-history-action="expense">Записать трату с этого счёта</button>');
    }
    actions.push(`<button class="secondary" type="button" data-history-action="adjust">${credit ? "Исправить долг" : "Исправить остаток"}</button>`);
    $("accountHistoryActions").innerHTML = actions.join("");
    const items = data?.items || [];
    $("accountHistoryList").innerHTML = items.length ? items.map(historyRow).join("")
      : '<div class="empty">Операций по счёту пока нет.</div>';
    if (!$("accountHistoryDialog").open) $("accountHistoryDialog").showModal();
  }

  function transferOptions(selected, { excludeCredit = true } = {}) {
    return active().filter(a => !excludeCredit || a.kind !== "credit")
      .map(a => `<option value="${a.id}"${a.id === Number(selected) ? " selected" : ""}>${accountName(a)} · ${money(a.balance)}</option>`).join("");
  }

  function openTransfer({ from = null, to = null, amount = "", kind = "manual", note = "", transfer = null } = {}) {
    if (!$("transferDialog")) return;
    const regular = active().filter(a => a.kind !== "credit");
    if (regular.length < 2 && !transfer) return notify("Для перевода нужны хотя бы два счёта");
    const source = transfer?.from_account_id ?? from ?? snap?.main_account_id ?? regular[0]?.id;
    const target = transfer?.to_account_id ?? to ?? regular.find(a => a.id !== Number(source))?.id;
    $("transferId").value = transfer?.id || "";
    $("transferKind").value = kind;
    $("transferTitle").textContent = kind === "hint" ? "Перевёл между счетами" : "Перевод между счетами";
    $("transferFrom").innerHTML = transferOptions(source);
    $("transferTo").innerHTML = transferOptions(target);
    $("transferFrom").value = String(source ?? "");
    $("transferTo").value = String(target ?? "");
    $("transferAmount").value = amount === "" ? "" : fmtInput(amount);
    $("transferDate").value = window.budgetDate?.today?.() || "";
    $("transferNote").value = note;
    $("transferDialog").showModal();
  }

  async function saveTransfer(event) {
    event?.preventDefault?.();
    const id = $("transferId").value;
    const body = {
      from_account_id: Number($("transferFrom").value), to_account_id: Number($("transferTo").value),
      amount: parseMoney($("transferAmount").value), date: $("transferDate").value, note: $("transferNote").value,
      kind: $("transferKind").value || "manual",
    };
    try {
      await request(id ? `/api/accounts/transfers/${id}` : "/api/accounts/transfers", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
      $("transferDialog").close();
      notify("Перевод записан");
      await afterChange();
    } catch (error) { notify(error.message); }
  }

  function openAdjust(id) {
    const account = byId(id);
    if (!account || !$("adjustDialog")) return;
    const credit = account.kind === "credit";
    $("adjustAccountId").value = account.id;
    $("adjustTitle").textContent = credit ? "Исправить долг" : "Исправить остаток";
    $("adjustLabel").textContent = credit ? "Долг по выписке банка" : "Сколько на счёте на самом деле";
    $("adjustCaption").textContent = `${account.emoji} ${account.title}: сейчас ${credit ? `долг ${money(account.debt)}` : money(account.balance)}`;
    $("adjustHint").textContent = credit
      ? "Долг вырос — запишутся проценты или комиссия банка, уменьшился — кэшбэк или возврат. Бюджет не меняется."
      : "Разница запишется исправлением остатка. Бюджет не меняется.";
    $("adjustTarget").value = fmtInput(credit ? account.debt : account.balance);
    $("adjustDate").value = window.budgetDate?.today?.() || "";
    $("adjustNote").value = "";
    $("adjustDialog").showModal();
  }

  async function saveAdjust(event) {
    event?.preventDefault?.();
    const id = $("adjustAccountId").value;
    try {
      await request(`/api/accounts/${id}/adjust`, { method: "POST", body: JSON.stringify({
        target: parseMoney($("adjustTarget").value), date: $("adjustDate").value, note: $("adjustNote").value,
      }) });
      $("adjustDialog").close();
      notify("Остаток исправлен");
      await afterChange();
    } catch (error) { notify(error.message); }
  }

  async function deleteFromHistory(key) {
    const [type, id] = key.split(":");
    const path = type === "transfer" ? `/api/accounts/transfers/${id}` : type === "adjustment" ? `/api/accounts/adjustments/${id}` : null;
    if (!path || !confirm("Удалить операцию? Остатки пересчитаются.")) return;
    try {
      await request(path, { method: "DELETE" });
      notify("Операция удалена");
      await afterChange();
    } catch (error) { notify(error.message); }
  }

  async function afterChange() {
    await reload();
    if (historyAccount && $("accountHistoryDialog")?.open) await openHistory(historyAccount.id);
  }

  function historyAction(action) {
    const account = historyAccount;
    if (!account) return;
    if (action === "transfer") return openTransfer({ from: account.id });
    if (action === "adjust") return openAdjust(account.id);
    if (action === "expense") {
      $("accountHistoryDialog").close();
      if (typeof openExpenseDialog === "function") openExpenseDialog();
      const select = $("expenseAccount");
      if (select) { select.value = String(account.id); updateHint("expense"); }
    }
  }

  // ---------- «Нужно перевести» ----------

  function hintRow(hint) {
    if (hint.type === "payroll_income") {
      const main = byId(hint.account_id);
      const what = hint.confirmed ? "не записана на счёт" : "не отмечена полученной";
      const date = `${hint.payday.slice(8, 10)}.${hint.payday.slice(5, 7)}`;
      return `<div class="acc-hint"><span>${esc(hint.title)} ${date} ${what} — записать приход ${money(hint.amount)} на ${main ? accountName(main) : "основной счёт"}?</span><button class="tiny" type="button" data-hint-income="${hint.period_start}">Записать приход</button></div>`;
    }
    if (hint.type === "transfer") {
      const from = byId(hint.from_account_id), to = byId(hint.to_account_id);
      return `<div class="acc-hint"><span>Переведи ${money(hint.amount)} с ${from ? accountName(from) : "счёта"} на ${to ? accountName(to) : "счёт"}</span><button class="tiny" type="button" data-hint-transfer="${hint.from_account_id}:${hint.to_account_id}:${hint.amount}">Перевёл</button></div>`;
    }
    return "";
  }

  async function confirmHintTransfer(key) {
    const [from, to, value] = key.split(":");
    try {
      await request("/api/accounts/transfers", { method: "POST", body: JSON.stringify({
        from_account_id: Number(from), to_account_id: Number(to), amount: Number(value),
        date: window.budgetDate?.today?.(), kind: "hint",
      }) });
      notify("Перевод записан");
      await reload();
    } catch (error) { notify(error.message); }
  }

  async function placeEnvelope(key, accountId) {
    const envelope = (snap?.envelopes || []).find(e => e.key === key);
    if (!envelope || Number(accountId) === envelope.account_id) return;
    const from = byId(envelope.account_id), to = byId(accountId);
    const amount = Number(envelope.amount) || 0;
    const move = amount > 0 && from && to
      && confirm(`Перевести ${money(amount)} (${envelope.label}) с «${from.title}» на «${to.title}» одним переводом?`);
    try {
      await request(`/api/accounts/envelopes/${key}`, { method: "PUT", body: JSON.stringify({
        account_id: Number(accountId), move_money: Boolean(move),
      }) });
      notify(move ? "Конверт и его деньги перенесены" : "Конверт перенесён");
      await reload();
    } catch (error) { notify(error.message); }
  }

  function renderHints() {
    const box = $("accHints");
    if (!box) return;
    const hints = enabled() ? (snap.hints || []) : [];
    box.innerHTML = hints.length
      ? `<details class="acc-hints"><summary>Нужно перевести (${hints.length})</summary>${hints.map(hintRow).join("")}</details>` : "";
  }

  async function recordPayrollIncome(periodStart) {
    try {
      await request("/api/accounts/payroll-income", { method: "POST", body: JSON.stringify({ period_start: periodStart }) });
      notify("Приход записан");
      await reload();
    } catch (error) { notify(error.message); }
  }

  // ---------- Drag and drop (the same behaviour as the categories) ----------

  const MOVE_MS = 200;
  let drag = null;
  const reduced = () => Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

  function slide(items, change) {
    const animate = !reduced() && items.every(item => item.animate);
    const before = animate ? items.map(item => item.getBoundingClientRect().top) : [];
    change();
    if (!animate) return;
    items.forEach((item, index) => {
      item.accountMove?.cancel();
      const shift = before[index] - item.getBoundingClientRect().top;
      if (!shift) return;
      item.accountMove = item.animate([{ transform: `translateY(${shift}px)` }, { transform: "none" }],
        { duration: MOVE_MS, easing: "cubic-bezier(.2,.8,.2,1)" });
    });
  }

  function beginDrag(list, target, inputId, clientY) {
    if (drag) return false;
    const handle = target?.closest?.(".category-drag-handle");
    const item = handle?.closest?.(".account-item");
    if (!item) return false;
    item.accountMove?.cancel();
    drag = { inputId, item, previous: active().map(a => a.id),
      grabOffset: clientY - list.getBoundingClientRect().top - item.offsetTop };
    item.classList.add("dragging");
    list.classList.add("sorting");
    return true;
  }

  function moveDrag(list, clientY, inputId) {
    if (!drag || drag.inputId !== inputId) return false;
    const { item } = drag;
    const top = clientY - list.getBoundingClientRect().top - drag.grabOffset;
    const center = top + item.offsetHeight / 2;
    const others = [...list.querySelectorAll(".account-item:not(.dragging)")];
    const before = others.find(other => center < other.offsetTop + other.offsetHeight / 2) || null;
    if (item.nextElementSibling !== before) slide(others, () => list.insertBefore(item, before));
    item.style.transform = `translateY(${top - item.offsetTop}px)`;
    if (clientY < 90) window.scrollBy(0, -12);
    else if (clientY > window.innerHeight - 90) window.scrollBy(0, 12);
    return true;
  }

  async function saveOrder(ids, previous) {
    if (ids.every((id, i) => id === previous[i])) return;
    const order = new Map(ids.map((id, i) => [id, i]));
    snap.accounts = [...active().sort((a, b) => order.get(a.id) - order.get(b.id)), ...archived()];
    renderHome();
    try {
      await request("/api/accounts/order", { method: "PUT", body: JSON.stringify({ account_ids: ids }) });
    } catch (error) {
      const back = new Map(previous.map((id, i) => [id, i]));
      snap.accounts = [...active().sort((a, b) => back.get(a.id) - back.get(b.id)), ...archived()];
      renderSettings();
      renderHome();
      notify(error.message);
    }
  }

  async function finishDrag(list, inputId, cancelled = false) {
    if (!drag || drag.inputId !== inputId) return;
    const current = drag;
    drag = null;
    list.classList.remove("sorting");
    if (cancelled) {
      current.item.classList.remove("dragging");
      return renderSettings();
    }
    slide([current.item], () => { current.item.style.transform = ""; });
    if (current.item.accountMove) current.item.accountMove.onfinish = () => current.item.classList.remove("dragging");
    else current.item.classList.remove("dragging");
    const ids = [...list.querySelectorAll(".account-item")].map(item => Number(item.dataset.accountId));
    await saveOrder(ids, current.previous);
  }

  function bindDrag(list) {
    list.addEventListener("pointerdown", event => {
      if (event.pointerType === "touch") return;
      const inputId = `pointer:${event.pointerId}`;
      if (!beginDrag(list, event.target, inputId, event.clientY)) return;
      event.preventDefault();
      list.setPointerCapture?.(event.pointerId);
    });
    list.addEventListener("pointermove", event => {
      if (moveDrag(list, event.clientY, `pointer:${event.pointerId}`)) event.preventDefault();
    });
    list.addEventListener("pointerup", event => {
      const inputId = `pointer:${event.pointerId}`;
      if (!drag || drag.inputId !== inputId) return;
      try { list.releasePointerCapture?.(event.pointerId); } catch (_) {}
      finishDrag(list, inputId);
    });
    list.addEventListener("pointercancel", event => finishDrag(list, `pointer:${event.pointerId}`, true));
    // Telegram's iOS webview can omit Pointer Events: classic touch events.
    list.addEventListener("touchstart", event => {
      if (drag) return;
      const touch = event.changedTouches[0];
      if (!touch || !beginDrag(list, event.target, `touch:${touch.identifier}`, touch.clientY)) return;
      event.preventDefault();
    }, { passive: false });
    list.addEventListener("touchmove", event => {
      if (!drag || !drag.inputId.startsWith("touch:")) return;
      const id = Number(drag.inputId.slice(6));
      const touch = [...event.touches].find(item => item.identifier === id);
      if (touch && moveDrag(list, touch.clientY, drag.inputId)) event.preventDefault();
    }, { passive: false });
    list.addEventListener("touchend", event => {
      if (!drag || !drag.inputId.startsWith("touch:")) return;
      const id = Number(drag.inputId.slice(6));
      if ([...event.changedTouches].some(item => item.identifier === id)) finishDrag(list, drag.inputId);
    });
    list.addEventListener("touchcancel", () => {
      if (drag?.inputId.startsWith("touch:")) finishDrag(list, drag.inputId, true);
    });
  }

  // ---------- Wiring ----------

  function bind() {
    $("accountsEnabled")?.addEventListener("change", toggleFeature);
    $("addAccountBtn")?.addEventListener("click", () => openAccount());
    $("accountForm")?.addEventListener("submit", saveAccount);
    $("accountKind")?.addEventListener("change", syncKindFields);
    $("archiveAccountBtn")?.addEventListener("click", () => archiveAccount($("accountId").value));
    $("deleteAccountBtn")?.addEventListener("click", () => deleteAccount($("accountId").value));
    $("accountsWizardNext")?.addEventListener("click", wizardNext);
    $("accountsWizardBack")?.addEventListener("click", wizardBack);
    $("accountsWizardBody")?.addEventListener("click", wizardClick);
    $("accountsWizardBody")?.addEventListener("change", wizardChange);
    for (const prefix of ["expense", "incomeTx", "irrIncome"]) {
      $(`${prefix}Account`)?.addEventListener("change", () => updateHint(prefix));
      $(`${prefix}Date`)?.addEventListener("change", () => updateHint(prefix));
    }
    $("transferForm")?.addEventListener("submit", saveTransfer);
    $("adjustForm")?.addEventListener("submit", saveAdjust);
    $("adjustAccountBtn")?.addEventListener("click", () => { $("accountDialog")?.close(); openAdjust($("accountId").value); });
    $("accountHistoryActions")?.addEventListener("click", event => {
      const button = event.target?.closest?.("[data-history-action]");
      if (button) historyAction(button.dataset.historyAction);
    });
    $("accountHistoryList")?.addEventListener("click", event => {
      const button = event.target?.closest?.("[data-history-delete]");
      if (button) deleteFromHistory(button.dataset.historyDelete);
    });
    const openFromHome = event => {
      const add = event.target?.closest?.("[data-add-to]");
      if (add) return addToEnvelope(add.dataset.addTo, Number(add.dataset.amount));
      const send = event.target?.closest?.("[data-transfer-to]");
      if (send) return transferTo(send.dataset.transferTo, Number(send.dataset.amount));
      const fix = event.target?.closest?.("[data-adjust-account]");
      if (fix) return openAdjust(fix.dataset.adjustAccount);
      const row = event.target?.closest?.("[data-account-id]");
      return row ? openHistory(row.dataset.accountId) : null;
    };
    $("accList")?.addEventListener("click", openFromHome);
    $("accCredit")?.addEventListener("click", openFromHome);
    $("accHints")?.addEventListener("click", event => {
      const income = event.target?.closest?.("[data-hint-income]");
      if (income) return recordPayrollIncome(income.dataset.hintIncome);
      const moved = event.target?.closest?.("[data-hint-transfer]");
      return moved ? confirmHintTransfer(moved.dataset.hintTransfer) : null;
    });
    $("envelopePlaces")?.addEventListener("change", event => {
      const key = event.target?.dataset?.envelope;
      return key ? placeEnvelope(key, event.target.value) : null;
    });
    $("expenseAccount")?.addEventListener("change", () => fillFunding());
    const list = $("accountsList");
    if (list) {
      list.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-edit-account]");
        if (button) openAccount(byId(button.dataset.editAccount));
      });
      bindDrag(list);
    }
    $("accountsArchive")?.addEventListener("click", event => {
      const fix = event.target?.closest?.("[data-adjust-account]");
      if (fix) return openAdjust(fix.dataset.adjustAccount);
      const restore = event.target?.closest?.("[data-restore-account]");
      if (restore) return restoreAccount(restore.dataset.restoreAccount);
      const remove = event.target?.closest?.("[data-delete-account]");
      if (remove) return deleteAccount(remove.dataset.deleteAccount);
    });
  }

  function render(state) {
    snap = state?.accounts && Array.isArray(state.accounts.accounts) ? state.accounts : null;
    renderHome();
    renderHints();
    renderSettings();
  }

  bind();
  window.accountsUi = {
    render, prepareExpense, expensePayload, prepareIncome, fieldPayload, prepareField, openAccount, openWizard,
    preparePiggy, prepareReserve,
    openHistory, openTransfer, openAdjust, mainAccountId: () => snap?.main_account_id ?? null,
    accountLabel(t) {
      if (!t?.account_title) return "";
      return `${esc(t.account_emoji)} ${esc(t.account_title)}${t.account_archived ? " (архив)" : ""}`;
    },
  };
})();
