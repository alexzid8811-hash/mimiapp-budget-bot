(() => {
  let bufferState = { buffer: null, piggy: { balance: 0, movements: [] } };
  // Closed periods of the plan are hidden until the user asks for them.
  let showPast = false;

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
      throw new Error(typeof apiErrorMessage === "function"
        ? apiErrorMessage(body, response.status)
        : (typeof body.detail === "string" && body.detail) || `Ошибка ${response.status}`);
    }
    return response.json();
  }

  function formatMoney(value) {
    const code = document.getElementById("currency")?.value || "RUB";
    return new Intl.NumberFormat("ru-RU", {
      style: "currency", currency: code, minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(Number(value || 0));
  }

  function formatDate(value, withYear = false) {
    const options = withYear ? { day: "numeric", month: "short", year: "numeric" } : { day: "numeric", month: "short" };
    return new Intl.DateTimeFormat("ru-RU", options).format(new Date(`${value}T12:00:00`));
  }

  // The start row has no payday: its money is the start capital, so it is
  // shown as received and the buffer movement is counted from zero.
  function withStartCapital(period) {
    if (period.payday !== null) return period;
    const capital = Number(period.buffer_start) || 0;
    const buffer = Number(period.buffer) || 0;
    const round = value => Math.round(value * 100) / 100;
    return {
      ...period,
      received: round(Number(period.received) + capital),
      free: round(Number(period.free) + capital),
      buffer_start: 0,
      put_aside: Math.max(0, buffer),
      take: Math.max(0, -buffer),
    };
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

  // "Изменить" is offered for the current period only; later paydays are
  // corrected once their period comes.
  function receivedMarkup(period, compact = false, current = false) {
    const value = `<span>${formatMoney(period.received)}</span>`;
    if (!period.received_editable) return compact ? value : `<b>${value}</b>`;
    const status = period.income_confirmed ? '<small class="income-status done">получено ✓</small>'
      : period.income_pending ? '<small class="income-status pending">ждём подтверждения</small>'
        : period.income_overridden ? '<small class="income-status">изменено</small>' : '';
    const edit = !current ? "" : `<button class="income-btn" type="button" onclick="editCashflowIncome('${period.override_key}')" aria-label="Изменить полученную сумму">Изменить</button>`;
    // The payday has come but the money is not confirmed yet: offer to enter
    // the amount actually received next to "Изменить".
    const confirm = period.income_pending
      ? `<button class="income-btn accent" type="button" onclick="confirmCashflowIncome('${period.override_key}')" aria-label="Подтвердить получение выплаты">Получил ✓</button>`
      : '';
    return `<span class="income-cell"><span class="income-amount">${formatMoney(period.received)}</span>${status}<span class="income-actions">${edit}${confirm}</span></span>`;
  }

  function renderBuffer() {
    const data = bufferState.buffer;
    if (!data) return;
    const disabled = document.getElementById("bufferDisabled");
    disabled?.classList.toggle("hidden", Boolean(data.enabled));
    setText("bufferHorizon", data.enabled ? `до ${formatDate(data.horizon_end, true)}` : "выключен");
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
    const periods = data.enabled ? (data.periods || []).map(withStartCapital) : [];
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
    // Red marks only the three smallest balances ahead; the current period is blue.
    const lowest = new Set(periods.map((p,i)=>({i,b:Number(p.buffer)||0})).filter(x=>x.i>0).sort((a,b)=>a.b-b.b).slice(0,3).map(x=>x.i));
    chart.innerHTML = periods.map((p,i) => `<div class="bar ${i===0?"cur":lowest.has(i)?"low":""}" style="height:${max>0?Math.max(3,Math.max(0,Number(p.buffer))/max*100):3}%" title="${safe(formatDate(p.start)+" — "+formatDate(p.end)+": "+formatMoney(p.buffer))}"></div>`).join("");
    const last = periods[periods.length-1];
    setText("bufChartEnd", formatDate(last.end)+": "+formatMoney(last.buffer));
    const worst = periods.map((p,i)=>({...p,index:i})).filter(p=>p.take>0).sort((a,b)=>b.take-a.take).slice(0,2).sort((a,b)=>a.index-b.index);
    if(worst.length) {
      const text = "Больше всего возьмёте из буфера в периоды "+worst.map(p=>formatDate(p.start)+" — "+formatDate(p.end)).join(" и ")+": "+formatMoney(worst.reduce((a,p)=>a+Number(p.take),0))+". К концу прогноза останется "+formatMoney(last.buffer)+".";
      warning.textContent=text;homeWarning.textContent=text;
      warning.classList.remove("hidden");homeWarning.classList.remove("hidden");
    }
    const movement = p => p.take>0
      ? ["neg",formatMoney(p.take),"из буфера"]
      : p.put_aside>0
        ? ["pos",formatMoney(p.put_aside),"в буфер"]
        : ["zero","—","без движения"];
    const toCardInner = p => `${formatMoney(p.to_card)}<br><small>${formatMoney(p.daily)} в день</small>`;
    const toCardLine = p => Number(p.to_card) > 0
      ? `<div><span>На карту</span><b>${toCardInner(p)}</b></div>` : "";
    const plural = n => n%100>=11&&n%100<=14?"дней":n%10===1?"день":n%10>=2&&n%10<=4?"дня":"дней";
    const past = (data.past_periods || []).map(withStartCapital).map(p => ({...p, past: true}));
    const pastToggle = document.getElementById("bufPastToggle");
    if (pastToggle) {
      pastToggle.hidden = !past.length;
      pastToggle.textContent = showPast ? "Скрыть прошлые периоды" : `Показать прошлые периоды (${past.length})`;
    }
    const shown = [...(showPast ? past : []), ...periods];
    const rowClass = (p, i) => p.past ? "past" : (i === 0 ? "cur" : "");
    // Cards follow the design: period and movement on top, the payout as its
    // own block (amount, status, "Изменить" / "Получил ✓"), then where the
    // money goes, then what stays in the buffer.
    const payoutBlock = (p, current) => {
      const head = `${safe(p.kind)}${p.payday ? ` ${formatDate(p.payday)}` : ""}`;
      if (!p.received_editable) {
        const done = p.past ? '<span class="pc-status done">получено ✓</span>' : "";
        return `<div class="pc-pay"><div class="pc-pay-top"><div><span>${head}</span><b class="num">${formatMoney(p.received)}</b></div>${done}</div></div>`;
      }
      const status = p.income_confirmed ? '<span class="pc-status done">получено ✓</span>'
        : p.income_pending ? '<span class="pc-status pending">ждём подтверждения</span>'
          : p.income_overridden ? '<span class="pc-status">изменено</span>' : "";
      const edit = !current ? "" : `<button class="income-btn" type="button" onclick="editCashflowIncome('${p.override_key}')" aria-label="Изменить полученную сумму">Изменить</button>`;
      const confirm = p.income_pending
        ? `<button class="income-btn accent" type="button" onclick="confirmCashflowIncome('${p.override_key}')" aria-label="Подтвердить получение выплаты">Получил ✓</button>`
        : "";
      const actions = edit || confirm ? `<div class="pc-pay-actions${edit && confirm ? " two" : ""}">${edit}${confirm}</div>` : "";
      return `<div class="pc-pay"><div class="pc-pay-top"><div><span>${head}</span><b class="num">${formatMoney(p.received)}</b></div>${status}</div>${actions}</div>`;
    };
    const groupTitle = (p, i, j) => {
      if (p.past) return j === 0 ? '<h4 class="pc-group">Прошедшие</h4>' : "";
      if (i === 0) return '<h4 class="pc-group">Сейчас</h4>';
      return i === 1 ? '<h4 class="pc-group">Следующие</h4>' : "";
    };
    cards.innerHTML = shown.map((p,j)=>{
      const i = j - (shown.length - periods.length);
      const [c,m,shortLabel]=movement(p);
      const label = c==="pos" ? "положить в буфер" : c==="neg" ? "взять из буфера" : shortLabel;
      const chip = p.past ? '<span class="pc-chip past">прошёл</span>' : i===0 ? '<span class="pc-chip">сейчас</span>' : "";
      const flow = `<div class="pc-flow pc-flow3 num">${toCardLine(p)}<div><span>Обязательные</span><b>${formatMoney(p.mandatory)}</b></div><div><span>Свободно</span><b>${formatMoney(p.free)}</b></div></div>`;
      const footLabel = p.past ? "Остаток буфера" : "Останется в буфере";
      return `${groupTitle(p,i,j)}<article class="pc ${rowClass(p,i)}"><div class="pc-top"><div><div class="pc-date">${formatDate(p.start)} — ${formatDate(p.end)}${chip}</div><div class="pc-kind">${Number(p.days)} ${plural(Number(p.days))}</div></div><div class="pc-move ${c} num">${m}<small>${label}</small></div></div>${payoutBlock(p,!p.past&&i===0)}${flow}<div class="pc-foot"><span>${footLabel}</span><b class="num ${p.buffer<max*.1?"low":""}">${formatMoney(p.buffer)}</b></div></article>`;
    }).join("");
    document.getElementById("bufferHead").innerHTML = "<tr>"+["Период и выплата","Дней","Деньги периода","Обязательные","Свободно","На карту","Движение буфера","Остаток буфера"].map(t=>`<th>${t}</th>`).join("")+"</tr>";
    body.innerHTML = shown.map((p,j)=>{
      const i = j - (shown.length - periods.length);
      const [c,m,label]=movement(p);
      const cells=[`<strong>${formatDate(p.start)} — ${formatDate(p.end)}</strong><br><small>${safe(p.kind)}</small>`,Number(p.days)];
      const tail=[formatMoney(p.mandatory),formatMoney(p.free)];
      const toCardCell=`<strong>${formatMoney(p.to_card)}</strong><br><small>${formatMoney(p.daily)} в день</small>`;
      return `<tr class="${rowClass(p,i)}">${cells.map(value=>`<td class="num">${value}</td>`).join("")}<td class="num">${receivedMarkup(p,true,!p.past&&i===0)}</td>${tail.map(value=>`<td class="num">${value}</td>`).join("")}<td class="num">${toCardCell}</td><td class="num sep ${c} movement-cell"><span>${m}</span><small>${label}</small></td><td class="num ${p.buffer<max*.1?"low":""}">${formatMoney(p.buffer)}</td></tr>`;
    }).join("");
  }

  document.getElementById("bufPastToggle")?.addEventListener("click", () => {
    showPast = !showPast;
    renderBuffer();
  });

  function openIncomeDialog(overrideKey, confirm) {
    const period = bufferState.buffer?.periods?.find(item => item.override_key === overrideKey);
    if (!period?.received_editable) return;
    document.getElementById("cashflowIncomeForm").dataset.mode = confirm ? "confirm" : "edit";
    setText("cashflowIncomeTitle", confirm ? "Подтвердить получение" : "Фактическая сумма выплаты");
    setText("cashflowIncomeLabel", confirm ? "Сколько пришло" : "Получено фактически");
    setText("saveCashflowIncomeBtn", confirm ? "Получил, пересчитать" : "Сохранить и пересчитать");
    document.getElementById("cashflowIncomePeriodStart").value = period.override_key;
    document.getElementById("cashflowIncomeAmount").value = period.payday_amount;
    document.getElementById("cashflowIncomeCaption").textContent =
      `${period.kind} ${formatDate(period.payday)} · по расчёту ${formatMoney(period.planned_payday_amount)}`;
    document.getElementById("resetCashflowIncomeBtn").hidden = confirm || !period.income_overridden;
    document.getElementById("cashflowIncomeDialog").showModal();
  }

  window.editCashflowIncome = overrideKey => openIncomeDialog(overrideKey, false);
  window.confirmCashflowIncome = overrideKey => openIncomeDialog(overrideKey, true);

  document.getElementById("cashflowIncomeForm")?.addEventListener("submit", async event => {
    event.preventDefault();
    const periodStart = document.getElementById("cashflowIncomePeriodStart").value;
    const raw = document.getElementById("cashflowIncomeAmount").value;
    const amount = window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(raw) : Number(raw);
    const confirm = document.getElementById("cashflowIncomeForm").dataset.mode === "confirm";
    try {
      await request(`/api/cashflow/income-overrides/${periodStart}`, {
        method: "PUT", body: JSON.stringify(confirm ? { amount, confirmed: true } : { amount }),
      });
      document.getElementById("cashflowIncomeDialog").close();
      if (typeof toast === "function") {
        toast(confirm ? "Получение подтверждено, бюджет пересчитан" : "Сумма выплаты изменена, бюджет пересчитан");
      }
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
    const list = document.getElementById("piggyMovements");
    if (!piggy.movements?.length) {
      list.innerHTML = '<div class="empty">В копилке пока нет операций.</div>';
      return;
    }
    list.innerHTML = piggy.movements.map(item => {
      const deposit = item.direction === "deposit";
      const toCard = !deposit && item.source === "daily_budget";
      const title = deposit && item.source === "daily_budget" ? "Из остатка дня" : deposit ? "Пополнение"
        : toCard ? (item.purpose === "transfer" ? "На карту · по дням периода" : "На карту · на сегодня") : "Снятие";
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
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  }

  // mode: "deposit" | "withdraw" | "toCard" | "coverOverspend".
  function openMovement(mode) {
    const direction = mode === "withdraw" ? "withdraw" : "deposit";
    const purpose = mode === "coverOverspend" ? "cover_overspend" : "transfer";
    document.getElementById("piggyDirection").value = direction;
    document.getElementById("piggySource").value = "external";
    document.getElementById("piggyPurpose").value = purpose;
    document.getElementById("piggyDialogTitle").textContent = {
      deposit: "Пополнить копилку", withdraw: "Снять из копилки",
      toCard: "Копилка → карта", coverOverspend: "Покрыть перерасход из копилки",
    }[mode];
    document.getElementById("savePiggyBtn").textContent = {
      deposit: "Пополнить", withdraw: "Снять", toCard: "Перевести", coverOverspend: "Покрыть",
    }[mode];
    document.getElementById("piggyForm").dataset.mode = mode;
    // A plain transfer to the card asks whether it is for today or for the
    // whole period; "today" is the usual case of a purchase paid from savings.
    document.getElementById("piggyPurposeField")?.classList.toggle("hidden", mode !== "toCard");
    const choice = document.getElementById("piggyPurposeChoice");
    if (choice) choice.value = "today";
    document.getElementById("piggyAmount").value = "";
    document.getElementById("piggyDate").value = window.budgetDate.today();
    document.getElementById("piggyNote").value = "";
    document.getElementById("piggyDialog").showModal();
  }

  document.getElementById("piggyDepositBtn")?.addEventListener("click", () => openMovement("deposit"));
  document.getElementById("piggyTransferBtn")?.addEventListener("click", () => {
    document.getElementById("piggyDirection").value = "deposit";
    document.getElementById("piggySource").value = "daily_budget";
    document.getElementById("piggyPurpose").value = "transfer";
    document.getElementById("piggyDialogTitle").textContent = "Перенести остаток дня";
    document.getElementById("savePiggyBtn").textContent = "Перенести";
    document.getElementById("piggyForm").dataset.mode = "dailyToPiggy";
    document.getElementById("piggyPurposeField")?.classList.add("hidden");
    document.getElementById("piggyAmount").value = "";
    document.getElementById("piggyDate").value = window.budgetDate.today();
    document.getElementById("piggyNote").value = "";
    document.getElementById("piggyDialog").showModal();
  });
  document.getElementById("piggyToCardBtn")?.addEventListener("click", () => openMovement("toCard"));
  document.getElementById("piggyWithdrawBtn")?.addEventListener("click", () => openMovement("withdraw"));
  window.openCoverOverspend = () => openMovement("coverOverspend");

  document.getElementById("piggyForm")?.addEventListener("submit", async event => {
    event.preventDefault();
    const mode = document.getElementById("piggyForm").dataset.mode || "deposit";
    const raw = document.getElementById("piggyAmount").value;
    const amount = window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(raw) : Number(raw);
    const movement_date = document.getElementById("piggyDate").value;
    const note = document.getElementById("piggyNote").value;
    const isToCard = mode === "toCard" || mode === "coverOverspend";
    const purpose = mode === "toCard"
      ? document.getElementById("piggyPurposeChoice")?.value || "today"
      : document.getElementById("piggyPurpose").value;
    const path = isToCard
      ? "/api/piggy-bank/to-card"
      : `/api/piggy-bank/${document.getElementById("piggyDirection").value}`;
    const body = isToCard
      ? { amount, movement_date, note, purpose }
      : { amount, movement_date, note, source: document.getElementById("piggySource").value };
    try {
      await request(path, { method: "POST", body: JSON.stringify(body) });
      document.getElementById("piggyDialog").close();
      if (typeof toast === "function") {
        toast({
          deposit: "Копилка пополнена", withdraw: "Снято из копилки",
          dailyToPiggy: "Перенесено в копилку", toCard: "Переведено на карту",
          coverOverspend: "Перерасход покрыт",
        }[mode] || "Сохранено");
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
