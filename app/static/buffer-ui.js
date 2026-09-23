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
    const periodStart = period.budget_start || period.start;
    const edit = `<button class="editable-money" type="button" onclick="editCashflowIncome('${periodStart}')" aria-label="Изменить полученную сумму">${value}${badge}</button>`;
    if (period.income_overridden) return edit;
    return `${edit}<button class="secondary confirm-income" type="button" onclick="confirmCashflowIncome('${periodStart}')">Подтвердить сумму</button>`;
  }

  function renderBuffer() {
    const data = bufferState.buffer;
    if (!data) return;
    const disabled = document.getElementById("bufferDisabled");
    disabled?.classList.toggle("hidden", Boolean(data.enabled));
    setText("bufferHorizon", data.enabled ? `до ${formatDate(data.horizon_end)}` : "выключен");
    setText("bufferPageBalance", data.enabled ? formatMoney(data.buffer_balance) : "—");
    setText("bufferPageDaily", data.enabled ? formatMoney(data.available_today) : "—");
    const shortfall = document.getElementById("bufferShortfall");
    if (data.enabled && Number(data.capital_shortfall || 0) > 0) {
      shortfall.textContent = `Даже без повседневных трат не хватает ${formatMoney(data.capital_shortfall)}. Увеличьте стартовый капитал или скорректируйте обязательные платежи.`;
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
    const periods = data.enabled ? data.buffer_periods || data.periods || [] : [];
    if (!periods.length) {
      const message = data.enabled ? "Нет периодов для прогноза." : "План появится после включения расчёта в настройках.";
      body.innerHTML = `<tr><td colspan="8"><div class="empty">${message}</div></td></tr>`;
      cards.innerHTML = `<div class="empty">${message}</div>`;
      document.getElementById("bufferHead").innerHTML = "";
      return;
    }
    const calculationPeriods = periods.filter(p => !p.initial_capital && !p.historical_payout);
    const max = Math.max(0, ...calculationPeriods.map(p => Number(p.buffer) || 0));
    const commonDaily = calculationPeriods.length && calculationPeriods.every(p => Number(p.daily) === Number(calculationPeriods[0].daily));
    setText("planDaily", commonDaily ? "В день везде " + formatMoney(calculationPeriods[0].daily) : "Дневной лимит по периодам");
    chart.innerHTML = periods.map((p,i) => `<div class="bar ${i===0?"cur":p.buffer<max*.1?"low":""}" style="height:${max>0?Math.max(3,Math.max(0,Number(p.buffer))/max*100):3}%" title="${safe(formatDate(p.start)+" — "+formatDate(p.end)+": "+formatMoney(p.buffer))}"></div>`).join("");
    const last = calculationPeriods[calculationPeriods.length-1] || periods[periods.length-1];
    setText("bufChartEnd", formatDate(last.end)+": "+formatMoney(last.buffer));
    const worst = calculationPeriods.map((p,i)=>({...p,index:i})).filter(p=>p.take>0).sort((a,b)=>b.take-a.take).slice(0,2).sort((a,b)=>a.index-b.index);
    if(worst.length) {
      const text = "Больше всего возьмёте из буфера в периоды "+worst.map(p=>formatDate(p.start)+" — "+formatDate(p.end)).join(" и ")+": "+formatMoney(worst.reduce((a,p)=>a+Number(p.take),0))+". К концу прогноза останется "+formatMoney(last.buffer)+".";
      warning.textContent=text;homeWarning.textContent=text;
      warning.classList.remove("hidden");homeWarning.classList.remove("hidden");
    }
    const movement = p => p.initial_capital
      ? ["zero","—","начало расчёта"]
      : p.take>0
      ? ["neg",formatMoney(p.take),"из буфера"]
      : p.put_aside>0
        ? ["pos",formatMoney(p.put_aside),"в буфер"]
        : ["zero","—","без движения"];
    const plural = n => n%100>=11&&n%100<=14?"дней":n%10===1?"день":n%10>=2&&n%10<=4?"дня":"дней";
    cards.innerHTML = periods.map((p,i)=>{
      const [c,m,label]=movement(p);
      if (p.initial_capital) {
        return `<article class="pc ${i===0?"cur":""}"><div class="pc-top"><div><div class="pc-date">${formatDate(p.start)}</div><div class="pc-kind">Стартовый капитал</div></div><div class="pc-move ${c} num">${m}<small>${label}</small></div></div><div class="pc-flow num"><div><span>Стартовый капитал</span><b>${formatMoney(p.received)}</b></div><div><span>Старт расчёта</span><b>${formatDate(p.start)}</b></div></div></article>`;
      }
      if (p.historical_payout) {
        const status = p.income_overridden ? "фактическая выплата" : "расчётная выплата — уточните сумму";
        return `<article class="pc"><div class="pc-top"><div><div class="pc-date">${formatDate(p.start)}</div><div class="pc-kind">${safe(p.kind)} · ${status}</div></div></div><div class="pc-flow num"><div><span>${p.income_overridden ? "Получено" : "По расчёту"}</span>${receivedMarkup(p)}</div></div></article>`;
      }
      const carryover = Number(p.carryover || 0) > 0 ? `<div><span>Перенос с прошлого периода</span><b>${formatMoney(p.carryover)}</b></div>` : "";
      return `<article class="pc ${p.kind==="сейчас"?"cur":""}"><div class="pc-top"><div><div class="pc-date">${formatDate(p.start)} — ${formatDate(p.end)}</div><div class="pc-kind">${safe(p.kind)}, ${Number(p.days)} ${plural(Number(p.days))}</div></div><div class="pc-move ${c} num">${m}<small>${label}</small></div></div><div class="pc-flow num">${carryover}<div><span>${p.kind==="сейчас"?"Доступно сейчас":"Получено"}</span>${receivedMarkup(p)}</div><div><span>Обязательные</span><b>${formatMoney(p.mandatory)}</b></div><div><span>Свободно</span><b>${formatMoney(p.free)}</b></div></div><div class="pc-foot"><span>В день</span><b>${formatMoney(p.daily)}</b></div><div class="pc-foot"><span>Остаток буфера</span><b class="num ${p.buffer<max*.1?"low":""}">${formatMoney(p.buffer)}</b></div></article>`;
    }).join("");
    document.getElementById("bufferHead").innerHTML = "<tr>"+["Период и выплата","Дней","Перенос","Деньги периода","Обязательные","Свободно","В день","Движение буфера","Остаток буфера"].map(t=>`<th>${t}</th>`).join("")+"</tr>";
    body.innerHTML = periods.map((p,i)=>{
      const [c,m,label]=movement(p);
      if (p.initial_capital) {
        return `<tr class="${i===0?"cur":""}"><td><strong>${formatDate(p.start)}</strong><br><small>Стартовый капитал</small></td><td class="num">—</td><td class="num">—</td><td class="num"><b>${formatMoney(p.received)}</b></td><td class="num">—</td><td class="num"><b>${formatMoney(p.free)}</b></td><td class="num">—</td><td class="num sep ${c} movement-cell"><span>${m}</span><small>${label}</small></td><td class="num">—</td></tr>`;
      }
      if (p.historical_payout) {
        const status = p.income_overridden ? "фактическая выплата" : "расчётная выплата — уточните сумму";
        return `<tr><td><strong>${formatDate(p.start)}</strong><br><small>${safe(p.kind)} · ${status}</small></td><td class="num">—</td><td class="num">—</td><td class="num">${receivedMarkup(p,true)}</td><td class="num">—</td><td class="num">—</td><td class="num">—</td><td class="num sep">—</td><td class="num">—</td></tr>`;
      }
      const cells=[`<strong>${formatDate(p.start)} — ${formatDate(p.end)}</strong><br><small>${safe(p.kind)}</small>`,Number(p.days)];
      const tail=[formatMoney(p.mandatory),formatMoney(p.free),formatMoney(p.daily)];
      return `<tr class="${i===0?"cur":""}">${cells.map(value=>`<td class="num">${value}</td>`).join("")}<td class="num">${Number(p.carryover || 0) ? formatMoney(p.carryover) : "—"}</td><td class="num">${receivedMarkup(p,true)}</td>${tail.map(value=>`<td class="num">${value}</td>`).join("")}<td class="num sep ${c} movement-cell"><span>${m}</span><small>${label}</small></td><td class="num ${p.buffer<max*.1?"low":""}">${formatMoney(p.buffer)}</td></tr>`;
    }).join("");
  }

  window.editCashflowIncome = periodStart => {
    const period = bufferState.buffer?.periods?.find(item => item.budget_start === periodStart || item.start === periodStart);
    if (!period?.received_editable) return;
    document.getElementById("cashflowIncomePeriodStart").value = period.budget_start || period.start;
    document.getElementById("cashflowIncomeAmount").value = period.received;
    document.getElementById("cashflowIncomeCaption").textContent =
      `${formatDate(period.start)} — ${formatDate(period.end)} · по расчёту ${formatMoney(period.planned_received)}`;
    document.getElementById("resetCashflowIncomeBtn").hidden = !period.income_overridden;
    document.getElementById("cashflowIncomeDialog").showModal();
  };

  window.confirmCashflowIncome = async periodStart => {
    const period = bufferState.buffer?.periods?.find(item => item.budget_start === periodStart || item.start === periodStart);
    if (!period?.received_editable) return;
    try {
      await request(`/api/cashflow/income-overrides/${period.budget_start || period.start}`, {
        method: "PUT", body: JSON.stringify({ amount: Number(period.planned_received) }),
      });
      if (typeof toast === "function") toast("Расчётная выплата подтверждена, дневной бюджет пересчитан");
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
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

  function renderPiggy() {
    const piggy = bufferState.piggy || { balance: 0, movements: [] };
    setText("piggyBalance", formatMoney(piggy.balance));
    const withdraw = document.getElementById("piggyWithdrawBtn");
    if (withdraw) withdraw.disabled = Number(piggy.balance || 0) <= 0;
    const toCard = document.getElementById("piggyToCardBtn");
    if (toCard) toCard.disabled = Number(piggy.balance || 0) <= 0;
    const list = document.getElementById("piggyMovements");
    if (!piggy.movements?.length) {
      list.innerHTML = '<div class="empty">В копилке пока нет операций.</div>';
      return;
    }
    list.innerHTML = piggy.movements.map(item => {
      const deposit = item.direction === "deposit";
      const title = item.source === "daily_budget" && deposit ? "Из остатка дня"
        : item.source === "daily_budget" ? "Перевод на карту"
        : deposit ? "Пополнение" : "Снятие";
      return `<div class="list-row"><div class="row-main"><div class="movement-icon ${item.direction}">${deposit ? "+" : "−"}</div><div class="row-text"><div class="row-title">${title}</div><div class="row-sub">${formatDate(item.movement_date)}${item.note ? ` · ${safe(item.note)}` : ""}</div></div></div><div><div class="amount ${deposit ? "income" : "expense"}">${deposit ? "+" : "−"}${formatMoney(item.amount)}</div><div class="actions"><button class="tiny danger" type="button" onclick="deletePiggyMovement(${item.id})">Удалить</button></div></div></div>`;
    }).join("");
  }

  async function refresh(flow) {
    try {
      const [buffer, piggy] = await Promise.all([
        flow ? Promise.resolve(flow) : request("/api/buffer"), request("/api/piggy-bank"),
      ]);
      bufferState = { buffer, piggy };
      renderBuffer();
      renderPiggy();
      renderOverspendActions();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  }

  function openMovement(direction, source = "external", initialAmount = "") {
    document.getElementById("piggyDirection").value = direction;
    document.getElementById("piggySource").value = source;
    const isToCard = source === "daily_budget" && direction === "withdraw";
    document.getElementById("piggyDialogTitle").textContent = isToCard ? "Взять из копилки на карту"
      : source === "daily_budget" ? "Перенести остаток дня" : direction === "deposit" ? "Пополнить копилку" : "Снять из копилки";
    document.getElementById("savePiggyBtn").textContent = isToCard ? "Перевести на карту"
      : source === "daily_budget" ? "Перенести" : direction === "deposit" ? "Пополнить" : "Снять";
    document.getElementById("piggyAmount").value = initialAmount;
    document.getElementById("piggyDate").value = window.budgetDate.today();
    document.getElementById("piggyNote").value = "";
    document.getElementById("piggyDialog").showModal();
  }

  document.getElementById("piggyDepositBtn")?.addEventListener("click", () => openMovement("deposit"));
  document.getElementById("piggyTransferBtn")?.addEventListener("click", () => openMovement("deposit", "daily_budget"));
  document.getElementById("piggyToCardBtn")?.addEventListener("click", () => openMovement("withdraw", "daily_budget"));
  document.getElementById("piggyWithdrawBtn")?.addEventListener("click", () => openMovement("withdraw"));
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
        toast(direction === "deposit" ? "Копилка пополнена" : document.getElementById("piggySource").value === "daily_budget" ? "Деньги переведены на карту" : "Снято из копилки");
      }
      await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  });

  function renderOverspendActions() {
    const flow = bufferState.buffer;
    const wrap = document.getElementById("overspendActions");
    if (!wrap) return;
    const deficit = Math.max(0, -Number(flow?.available_today || 0));
    wrap.classList.toggle("hidden", deficit <= 0);
    if (!deficit) return;
    setText("overspendCaption", `Перерасход ${formatMoney(deficit)}. Выберите, как его покрыть; буфер не используется.`);
    const button = document.getElementById("coverOverspendFromPiggyBtn");
    if (button) button.disabled = Number(bufferState.piggy?.balance || 0) <= 0;
  }

  document.getElementById("spreadOverspendBtn")?.addEventListener("click", () => {
    if (typeof toast === "function") toast("Перерасход уже распределён по оставшимся дням периода");
  });
  document.getElementById("coverOverspendFromPiggyBtn")?.addEventListener("click", () => {
    const deficit = Math.max(0, -Number(bufferState.buffer?.available_today || 0));
    openMovement("withdraw", "daily_budget", deficit ? String(deficit.toFixed(2)) : "");
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
