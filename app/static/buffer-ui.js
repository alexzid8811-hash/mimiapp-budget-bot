(() => {
  let bufferState = { buffer: null, piggy: { balance: 0, movements: [] } };

  function initData() {
    if (typeof getTelegramInitData === "function") return getTelegramInitData();
    return window.Telegram?.WebApp?.initData || "";
  }

  async function request(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    const value = initData();
    if (value) headers["X-Telegram-Init-Data"] = value;
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Ошибка ${response.status}`);
    }
    return response.json();
  }

  function formatMoney(value) {
    const code = document.getElementById("currency")?.value || "RUB";
    return new Intl.NumberFormat("ru-RU", {
      style: "currency", currency: code, minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(Number(value || 0));
  }

  function formatDate(value) {
    return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" })
      .format(new Date(`${value}T12:00:00`));
  }

  function safe(value) {
    if (typeof escapeHtml === "function") return escapeHtml(value);
    return String(value ?? "").replace(/[&<>'"]/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[char]);
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function receivedMarkup(period, compact = false) {
    const value = `<span>${formatMoney(period.received)}</span>`;
    if (!period.received_editable) return compact ? value : `<b>${value}</b>`;
    const badge = period.income_overridden ? '<small class="edited-badge">изменено</small>' : '';
    return `<button class="editable-money" type="button" onclick="editCashflowIncome('${period.start}')" aria-label="Изменить полученную сумму">${value}${badge}</button>`;
  }

  function setHidden(id, hidden) {
    document.getElementById(id)?.classList.toggle("hidden", hidden);
  }

  function renderBufferMovements(data) {
    const list = document.getElementById("bufferMovements");
    if (!list) return;
    const movements = data.movements || [];
    if (!movements.length) {
      list.innerHTML = '<div class="empty">После настройки здесь появятся переводы и доходы, направленные в буфер.</div>';
      return;
    }
    list.innerHTML = movements.map(item => {
      const deposit = item.direction === "deposit";
      const title = item.source === "income"
        ? "Доход направлен в буфер"
        : item.source === "adjustment"
          ? "Уточнён фактический остаток"
          : deposit ? "Перевод с карты в буфер" : "Перевод из буфера на карту";
      return `<div class="list-row"><div class="row-main"><div class="movement-icon ${item.direction}">${deposit ? "+" : "−"}</div><div class="row-text"><div class="row-title">${title}</div><div class="row-sub">${formatDate(item.movement_date)}${item.note ? ` · ${safe(item.note)}` : ""}</div></div></div><div class="amount ${deposit ? "income" : "expense"}">${deposit ? "+" : "−"}${formatMoney(item.amount)}</div></div>`;
    }).join("");
  }

  function renderBuffer() {
    const data = bufferState.buffer;
    if (!data) return;
    const physical = data.buffer_is_physical === true;
    const needsSetup = data.buffer_setup_required === true;
    const disabled = document.getElementById("bufferDisabled");
    disabled?.classList.toggle("hidden", Boolean(data.enabled));
    setHidden("bufferSetupCard", !(data.enabled && needsSetup));
    setHidden("bufferAccountCard", !(data.enabled && physical));
    setHidden("bufferMovementCard", !(data.enabled && physical));
    setText("bufferIntro", physical
      ? "Буфер — отдельный счёт. Обычные траты не меняют его остаток: он изменяется только после явного перевода с карты, возврата на карту или дохода, направленного в буфер."
      : needsSetup
        ? "Сейчас показан старый расчётный буфер. Укажите фактический остаток отдельного счёта, чтобы обычные траты больше не меняли эту сумму."
        : "Приложение откладывает деньги из периодов с большими выплатами для будущих обязательных платежей и периодов с меньшими выплатами. Деньги остаются на вашем счёте.");
    setText("bufferPageBalanceTitle", physical ? "В буфере (факт)" : needsSetup ? "Расчётный буфер" : "В буфере сейчас");
    setText("bufferPlanTitle", physical ? "Прогноз по карте и обязательным платежам" : "План по периодам");
    setText("bufChartLabel", physical ? "Остаток буфера (факт)" : "Остаток буфера");
    setText("bufferHorizon", data.enabled ? `до ${formatDate(data.horizon_end)}` : "выключен");
    setText("bufferPageBalance", data.enabled ? formatMoney(data.buffer_balance) : "—");
    setText("bufferPageDaily", data.enabled ? formatMoney(data.available_today) : "—");

    if (needsSetup && data.enabled) {
      setText("bufferSetupHint", `Прогноз ранее показывал ${formatMoney(data.suggested_buffer_balance)}. Введите реальную сумму на отдельном счёте — она не будет меняться от обычных трат.`);
    }
    if (physical && data.enabled) {
      setText("bufferAccountBalance", formatMoney(data.buffer_balance));
      setText("bufferCardBalance", formatMoney(data.card_balance));
      setText("bufferCardAvailable", formatMoney(data.available_cash));
      const to = document.getElementById("bufferTransferToBtn");
      const from = document.getElementById("bufferTransferFromBtn");
      if (to) to.disabled = Number(data.available_cash || 0) <= 0;
      if (from) from.disabled = Number(data.buffer_balance || 0) <= 0;
      renderBufferMovements(data);
    }

    const shortfall = document.getElementById("bufferShortfall");
    if (data.enabled && Number(data.capital_shortfall || 0) > 0) {
      shortfall.textContent = physical
        ? `На карте не хватает ${formatMoney(data.capital_shortfall)} даже без повседневных трат. При необходимости верните эту сумму из буфера вручную.`
        : `Даже без повседневных трат не хватает ${formatMoney(data.capital_shortfall)}. Увеличьте стартовый капитал или скорректируйте обязательные платежи.`;
      shortfall.classList.remove("hidden");
    } else {
      shortfall.classList.add("hidden");
    }

    const body = document.getElementById("bufferPeriods");
    const cards = document.getElementById("planCards");
    const chart = document.getElementById("bufChart");
    const warning = document.getElementById("bufWarn");
    const homeWarning = document.getElementById("bufferForecastWarning");
    warning.classList.add("hidden"); homeWarning.classList.add("hidden");
    chart.innerHTML = ""; setText("bufChartEnd", "—"); setText("planDaily", "");
    const periods = data.enabled ? data.periods || [] : [];
    if (!periods.length) {
      const message = data.enabled ? "Нет периодов для прогноза." : "План появится после включения расчёта в настройках.";
      body.innerHTML = `<tr><td colspan="8"><div class="empty">${message}</div></td></tr>`;
      cards.innerHTML = `<div class="empty">${message}</div>`;
      document.getElementById("bufferHead").innerHTML = "";
      return;
    }
    const max = Math.max(0, ...periods.map(p => Number(p.buffer) || 0));
    const commonDaily = periods.every(p => Number(p.daily) === Number(periods[0].daily));
    setText("planDaily", commonDaily ? "В день везде " + formatMoney(periods[0].daily) : "Дневной лимит по периодам");
    chart.innerHTML = periods.map((p,i) => `<div class="bar ${i===0?"cur":p.buffer<max*.1?"low":""}" style="height:${max>0?Math.max(3,Math.max(0,Number(p.buffer))/max*100):3}%" title="${safe(formatDate(p.start)+" — "+formatDate(p.end)+": "+formatMoney(p.buffer))}"></div>`).join("");
    const last = periods[periods.length-1];
    setText("bufChartEnd", formatDate(last.end)+": "+formatMoney(last.buffer));
    const worst = physical ? [] : periods.map((p,i)=>({...p,index:i})).filter(p=>p.take>0).sort((a,b)=>b.take-a.take).slice(0,2).sort((a,b)=>a.index-b.index);
    if(worst.length) {
      const text = "Больше всего возьмёте из буфера в периоды "+worst.map(p=>formatDate(p.start)+" — "+formatDate(p.end)).join(" и ")+": "+formatMoney(worst.reduce((a,p)=>a+Number(p.take),0))+". К концу прогноза останется "+formatMoney(last.buffer)+".";
      warning.textContent=text;homeWarning.textContent=text;
      warning.classList.remove("hidden");homeWarning.classList.remove("hidden");
    }
    const movement = p => p.take>0
      ? ["neg",formatMoney(p.take),"из буфера"]
      : p.put_aside>0
        ? ["pos",formatMoney(p.put_aside),"в буфер"]
        : ["zero","—",physical ? "без автоперевода" : "без движения"];
    const plural = n => n%100>=11&&n%100<=14?"дней":n%10===1?"день":n%10>=2&&n%10<=4?"дня":"дней";
    cards.innerHTML = periods.map((p,i)=>{
      const [c,m,label]=movement(p);
      return `<article class="pc ${i===0?"cur":""}"><div class="pc-top"><div><div class="pc-date">${formatDate(p.start)} — ${formatDate(p.end)}</div><div class="pc-kind">${safe(p.kind)}, ${Number(p.days)} ${plural(Number(p.days))}</div></div><div class="pc-move ${c} num">${m}<small>${label}</small></div></div><div class="pc-flow num"><div><span>${i===0?"Доступно сейчас":"Получено"}</span>${receivedMarkup(p)}</div><div><span>Обязательные</span><b>${formatMoney(p.mandatory)}</b></div><div><span>Свободно</span><b>${formatMoney(p.free)}</b></div></div><div class="pc-foot"><span>В день</span><b>${formatMoney(p.daily)}</b></div><div class="pc-foot"><span>${physical ? "Буфер (факт)" : "Остаток буфера"}</span><b class="num ${p.buffer<max*.1?"low":""}">${formatMoney(p.buffer)}</b></div></article>`;
    }).join("");
    const movementTitle = physical ? "Автоперевод" : "Движение буфера";
    document.getElementById("bufferHead").innerHTML = "<tr>"+["Период и выплата","Дней","Деньги периода","Обязательные","Свободно","В день",movementTitle,physical ? "Буфер (факт)" : "Остаток буфера"].map(t=>`<th>${t}</th>`).join("")+"</tr>";
    body.innerHTML = periods.map((p,i)=>{
      const [c,m,label]=movement(p);
      const cells=[`<strong>${formatDate(p.start)} — ${formatDate(p.end)}</strong><br><small>${safe(p.kind)}</small>`,Number(p.days)];
      const tail=[formatMoney(p.mandatory),formatMoney(p.free),formatMoney(p.daily)];
      return `<tr class="${i===0?"cur":""}">${cells.map(value=>`<td class="num">${value}</td>`).join("")}<td class="num">${receivedMarkup(p,true)}</td>${tail.map(value=>`<td class="num">${value}</td>`).join("")}<td class="num sep ${c} movement-cell"><span>${m}</span><small>${label}</small></td><td class="num ${p.buffer<max*.1?"low":""}">${formatMoney(p.buffer)}</td></tr>`;
    }).join("");
  }

  window.editCashflowIncome = periodStart => {
    const period = bufferState.buffer?.periods?.find(item => item.start === periodStart);
    if (!period?.received_editable) return;
    document.getElementById("cashflowIncomePeriodStart").value = period.start;
    document.getElementById("cashflowIncomeAmount").value = period.received;
    document.getElementById("cashflowIncomeCaption").textContent =
      `${formatDate(period.start)} — ${formatDate(period.end)} · по расчёту ${formatMoney(period.planned_received)}`;
    document.getElementById("resetCashflowIncomeBtn").hidden = !period.income_overridden;
    document.getElementById("cashflowIncomeDialog").showModal();
  };

  document.getElementById("cashflowIncomeForm")?.addEventListener("submit", async event => {
    event.preventDefault();
    const periodStart = document.getElementById("cashflowIncomePeriodStart").value;
    const raw = document.getElementById("cashflowIncomeAmount").value;
    const amount = window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(raw) : Number(raw);
    try {
      await request(`/api/cashflow/income-overrides/${periodStart}`, {
        method: "PUT", body: JSON.stringify({ amount }),
      });
      document.getElementById("cashflowIncomeDialog").close();
      if (typeof toast === "function") toast("Сумма выплаты изменена, бюджет пересчитан");
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  });

  document.getElementById("resetCashflowIncomeBtn")?.addEventListener("click", async () => {
    const periodStart = document.getElementById("cashflowIncomePeriodStart").value;
    try {
      await request(`/api/cashflow/income-overrides/${periodStart}`, { method: "DELETE" });
      document.getElementById("cashflowIncomeDialog").close();
      if (typeof toast === "function") toast("Возвращён автоматический расчёт выплаты");
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  });

  function moneyInput(id) {
    const raw = document.getElementById(id)?.value || "0";
    return window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(raw) : Number(raw);
  }

  async function saveActualBufferBalance(balanceId, noteId, dialogId = null) {
    const balance = moneyInput(balanceId);
    if (!Number.isFinite(balance) || balance < 0) {
      if (typeof toast === "function") toast("Введите корректную сумму");
      return;
    }
    try {
      await request("/api/buffer/account-balance", {
        method: "PUT",
        body: JSON.stringify({ balance, note: document.getElementById(noteId)?.value || "" }),
      });
      if (dialogId) document.getElementById(dialogId)?.close();
      if (typeof toast === "function") toast("Фактический остаток буфера сохранён");
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  }

  document.getElementById("saveBufferActualBalanceBtn")?.addEventListener("click", () =>
    saveActualBufferBalance("bufferActualBalance", "bufferActualNote")
  );

  document.getElementById("bufferCorrectBtn")?.addEventListener("click", () => {
    const data = bufferState.buffer;
    if (!data?.buffer_is_physical) return;
    document.getElementById("bufferCorrectionBalance").value = Number(data.buffer_balance || 0).toFixed(2);
    document.getElementById("bufferCorrectionNote").value = "";
    document.getElementById("bufferBalanceDialog")?.showModal();
  });

  document.getElementById("bufferBalanceForm")?.addEventListener("submit", event => {
    event.preventDefault();
    saveActualBufferBalance("bufferCorrectionBalance", "bufferCorrectionNote", "bufferBalanceDialog");
  });

  function openBufferTransfer(direction) {
    const data = bufferState.buffer;
    if (!data?.buffer_is_physical) return;
    document.getElementById("bufferTransferDirection").value = direction;
    document.getElementById("bufferTransferDialogTitle").textContent =
      direction === "deposit" ? "Перевести с карты в буфер" : "Вернуть из буфера на карту";
    document.getElementById("saveBufferTransferBtn").textContent =
      direction === "deposit" ? "Перевести в буфер" : "Вернуть на карту";
    document.getElementById("bufferTransferAmount").value = "";
    document.getElementById("bufferTransferNote").value = "";
    document.getElementById("bufferTransferDialog")?.showModal();
  }

  document.getElementById("bufferTransferToBtn")?.addEventListener("click", () => openBufferTransfer("deposit"));
  document.getElementById("bufferTransferFromBtn")?.addEventListener("click", () => openBufferTransfer("withdraw"));
  document.getElementById("bufferTransferForm")?.addEventListener("submit", async event => {
    event.preventDefault();
    const direction = document.getElementById("bufferTransferDirection").value;
    const amount = moneyInput("bufferTransferAmount");
    if (!Number.isFinite(amount) || amount <= 0) {
      if (typeof toast === "function") toast("Введите сумму больше нуля");
      return;
    }
    try {
      await request(`/api/buffer/${direction === "deposit" ? "transfer-to" : "transfer-from"}`, {
        method: "POST",
        body: JSON.stringify({ amount, note: document.getElementById("bufferTransferNote").value || "" }),
      });
      document.getElementById("bufferTransferDialog")?.close();
      if (typeof toast === "function") {
        toast(direction === "deposit" ? "Деньги переведены в буфер" : "Деньги возвращены на карту");
      }
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  });

  function renderPiggy() {
    const piggy = bufferState.piggy || { balance: 0, movements: [] };
    setText("piggyBalance", formatMoney(piggy.balance));
    const withdraw = document.getElementById("piggyWithdrawBtn");
    if (withdraw) withdraw.disabled = Number(piggy.balance || 0) <= 0;
    const list = document.getElementById("piggyMovements");
    if (!piggy.movements?.length) {
      list.innerHTML = '<div class="empty">В копилке пока нет операций.</div>';
      return;
    }
    list.innerHTML = piggy.movements.map(item => {
      const deposit = item.direction === "deposit";
      const title = deposit && item.source === "daily_budget" ? "Из остатка дня" : deposit ? "Пополнение" : "Снятие";
      return `<div class="list-row"><div class="row-main"><div class="movement-icon ${item.direction}">${deposit ? "+" : "−"}</div><div class="row-text"><div class="row-title">${title}</div><div class="row-sub">${formatDate(item.movement_date)}${item.note ? ` · ${safe(item.note)}` : ""}</div></div></div><div><div class="amount ${deposit ? "income" : "expense"}">${deposit ? "+" : "−"}${formatMoney(item.amount)}</div><div class="actions"><button class="tiny danger" type="button" onclick="deletePiggyMovement(${item.id})">Удалить</button></div></div></div>`;
    }).join("");
  }

  async function refresh(flow) {
    try {
      const [buffer, piggy] = await Promise.all([
        // The home screen already has the cash-flow snapshot.  A physical
        // buffer additionally needs its audit history, which is returned by
        // the dedicated endpoint.
        flow && flow.buffer_is_physical !== true ? Promise.resolve(flow) : request("/api/buffer"),
        request("/api/piggy-bank"),
      ]);
      bufferState = { buffer, piggy };
      renderBuffer();
      renderPiggy();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  }

  function openMovement(direction, source = "external") {
    document.getElementById("piggyDirection").value = direction;
    document.getElementById("piggySource").value = source;
    document.getElementById("piggyDialogTitle").textContent =
      source === "daily_budget" && direction === "deposit" ? "Перенести остаток дня" :
      direction === "deposit" ? "Пополнить копилку" : "Вернуть на карту";
    document.getElementById("savePiggyBtn").textContent =
      source === "daily_budget" && direction === "deposit" ? "Перенести" :
      direction === "deposit" ? "Пополнить" : "Вернуть на карту";
    document.getElementById("piggyAmount").value = "";
    document.getElementById("piggyDate").value = window.budgetDate.today();
    document.getElementById("piggyNote").value = "";
    document.getElementById("piggyDialog").showModal();
  }

  document.getElementById("piggyDepositBtn")?.addEventListener("click", () => openMovement("deposit"));
  document.getElementById("piggyTransferBtn")?.addEventListener("click", () => openMovement("deposit", "daily_budget"));
  document.getElementById("piggyWithdrawBtn")?.addEventListener("click", () => openMovement("withdraw", "daily_budget"));
  document.getElementById("piggyForm")?.addEventListener("submit", async event => {
    event.preventDefault();
    const direction = document.getElementById("piggyDirection").value;
    const raw = document.getElementById("piggyAmount").value;
    const amount = window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(raw) : Number(raw);
    try {
      await request(`/api/piggy-bank/${direction}`, {
        method: "POST",
        body: JSON.stringify({
          amount,
          movement_date: document.getElementById("piggyDate").value,
          note: document.getElementById("piggyNote").value,
          source: document.getElementById("piggySource").value,
        }),
      });
      document.getElementById("piggyDialog").close();
      if (typeof toast === "function") {
        toast(direction === "deposit" ? "Копилка пополнена" : "Деньги возвращены на карту");
      }
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  });

  window.deletePiggyMovement = async id => {
    if (!confirm("Удалить операцию копилки?")) return;
    try {
      await request(`/api/piggy-bank/movements/${id}`, { method: "DELETE" });
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  };

  document.querySelector('[data-nav="buffer"]')?.addEventListener("click", () => refresh());
  document.querySelector('[data-nav="piggy"]')?.addEventListener("click", () => refresh());
  window.refreshBuffer = refresh;
})();
