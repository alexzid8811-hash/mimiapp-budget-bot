(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  const rub = v => new Intl.NumberFormat("ru-RU", {style:"currency",currency:"RUB",maximumFractionDigits:0}).format(Math.round(Number(v)||0));
  const day = s => new Intl.DateTimeFormat("ru-RU", {day:"numeric",month:"short"}).format(new Date(s+"T12:00:00"));
  const SLOTS = 8;
  const KINDS = [
    {key:"daily", title:"Повседневные траты", color:"var(--kind-daily)"},
    {key:"bills", title:"Обязательные платежи", color:"var(--kind-bills)"},
    {key:"piggy", title:"Отложено в копилку", color:"var(--kind-piggy)"},
  ];
  const COMPARE = {month:"к прошлому месяцу", period:"к прошлому периоду", year:"к прошлому году"};
  const COMPARE_SAME_DAY = {month:"к этому дню прошлого месяца", period:"к этому дню прошлого периода", year:"к этому дню прошлого года"};
  let view = {mode:"month", anchor:null};
  let data = null;
  let loadId = 0;
  const open = new Set();

  async function request(path) {
    // Reuse the authenticated helper from app.js.
    if (typeof api === "function") return api(path);
    const res = await fetch(path);
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
    return res.json();
  }

  async function load() {
    const id = ++loadId;
    const params = new URLSearchParams({mode:view.mode});
    if (view.anchor) params.set("anchor", view.anchor);
    try {
      const result = await request(`/api/analytics?${params}`);
      if (id !== loadId) return;
      data = result;
      render();
    } catch (e) {
      if (id !== loadId) return;
      if (typeof toast === "function") toast(e.message);
      if (view.mode === "period") { view = {mode:"month", anchor:null}; load(); }
    }
  }

  // Colour follows the category's place in the user's list, never its rank.
  function categoryColor(id) {
    const cats = (typeof state !== "undefined" && state?.bootstrap?.categories) || [];
    const index = cats.findIndex(c => c.id === id);
    return index >= 0 && index < SLOTS ? `var(--cat-${index + 1})` : "var(--cat-other)";
  }

  function delta(current, previous) {
    if (!(previous > 0)) return null;
    return Math.round((current - previous) / previous * 100);
  }

  function render() {
    if (!data) return;
    document.querySelectorAll("[data-amode]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.amode === data.mode)));
    $("analyticsLabel").textContent = data.label;
    $("analyticsNext").disabled = !data.next_anchor;
    renderHero(); renderCategories(); renderDays(); renderMonths(); renderBills();
  }

  function renderHero() {
    const t = data.totals;
    $("analyticsTotalCaption").textContent = data.mode === "year" ? "Всего ушло за год" : data.mode === "period" ? "Всего ушло за период" : "Всего ушло за месяц";
    $("analyticsTotal").textContent = rub(t.total);
    const d = delta(t.total, data.previous_totals.total), badge = $("analyticsDelta");
    badge.classList.toggle("hidden", d === null);
    if (d !== null) {
      badge.className = `analytics-delta ${d > 0 ? "up" : "down"}`;
      badge.textContent = `${d > 0 ? "▲" : "▼"} ${Math.abs(d)}% ${(data.previous_partial ? COMPARE_SAME_DAY : COMPARE)[data.mode]}`;
    }
    $("analyticsStack").innerHTML = t.total > 0
      ? KINDS.filter(k => t[k.key] > 0).map(k => `<span style="flex:${t[k.key]};background:${k.color}"></span>`).join("")
      : "";
    $("analyticsKinds").innerHTML = KINDS.map(k => `<div class="analytics-kind"><i style="background:${k.color}"></i><span>${k.title}</span><span class="num">${rub(t[k.key])}</span><span class="pct num">${t.total > 0 ? Math.round(t[k.key] / t.total * 100) : 0}%</span></div>`).join("");
  }

  function renderCategories() {
    const cats = data.categories;
    if (!cats.length) { $("analyticsCategories").innerHTML = `<div class="empty">Расходов за этот период нет.</div>`; return; }
    const max = cats[0].amount || 1;
    const note = data.previous_totals.daily > 0
      ? `<p class="muted analytics-cats-note">Изменение ${(data.previous_partial ? COMPARE_SAME_DAY : COMPARE)[data.mode]}</p>` : "";
    $("analyticsCategories").innerHTML = note + cats.map((c, i) => {
      const key = String(c.id ?? "none"), expanded = open.has(key), d = delta(c.amount, c.previous);
      // Without any spending in the previous range there is nothing to compare with.
      const hadPrevious = data.previous_totals.daily > 0;
      const change = d === null ? (hadPrevious ? "новая" : "") : d === 0 ? "без изменений" : `${d > 0 ? "▲" : "▼"} ${Math.abs(d)}%`;
      const drill = expanded ? `<span class="analytics-drill">${c.top.map(op => `<span><span>${esc(op.note || "Без комментария")}, ${day(op.date)}</span><span class="num">${rub(op.amount)}</span></span>`).join("")}${c.count > c.top.length ? `<span><span>ещё ${c.count - c.top.length} опер.</span><span></span></span>` : ""}</span>` : "";
      return `<button type="button" class="analytics-cat" aria-expanded="${expanded}" data-cat="${esc(key)}">
        <span class="emoji">${esc(c.emoji)}</span>
        <span><span class="top"><span>${esc(c.title)}</span><strong class="num">${rub(c.amount)}</strong></span>
        <span class="bar" style="display:block"><b style="width:${Math.max(1, c.amount / max * 100)}%;background:${categoryColor(c.id)}"></b></span>
        <span class="meta num"><span>${Math.round(c.share * 100)}% трат</span><span class="${!d ? "" : d > 0 ? "up" : "down"}">${change}</span></span></span>
        ${drill}</button>`;
    }).join("");
  }

  // Rounds a grid step up to 1, 2, 2.5 or 5 × 10ⁿ so every tick label is a round number.
  function niceStep(v) {
    if (!(v > 0)) return 250;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  const compact = v => v >= 1000 ? `${String(+(v / 1000).toFixed(1)).replace(".", ",")}к` : String(v);

  // Column chart shared by the day and month views: one y-scale, thin bars,
  // 4px rounded tops, a recessive grid and a hover/tap tooltip.
  function barChart(host, values, labels, opts) {
    const W = 380, H = 160, L = 34, R = 4, T = 10, B = 22;
    const step4 = niceStep(Math.max(opts.line || 0, ...values) / 4), top = step4 * 4;
    const ticks = [0, step4, step4 * 2, step4 * 3, top];
    const step = (W - L - R) / Math.max(1, values.length), bw = Math.max(3, Math.min(step - 2, 40));
    const y = v => T + (H - T - B) * (1 - v / top);
    let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.aria)}">`;
    ticks.forEach(t => { s += `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)"/><text x="${L - 6}" y="${y(t) + 3}" text-anchor="end">${compact(t)}</text>`; });
    values.forEach((v, i) => {
      if (!(v > 0)) return;
      const x = L + i * step + (step - bw) / 2, yt = y(v), r = Math.min(4, bw / 2, H - B - yt);
      s += `<path d="M${x},${H - B}V${yt + r}q0,-${r} ${r},-${r}h${bw - 2 * r}q${r},0 ${r},${r}V${H - B}Z" fill="${opts.color(v, i)}"/>`;
    });
    labels.forEach((l, i) => { if (l) s += `<text x="${L + i * step + step / 2}" y="${H - 6}" text-anchor="middle">${esc(l)}</text>`; });
    if (opts.line) s += `<line x1="${L}" x2="${W - R}" y1="${y(opts.line)}" y2="${y(opts.line)}" stroke="${opts.lineColor}" stroke-width="1.5" stroke-dasharray="4 3"/>`;
    values.forEach((v, i) => { s += `<rect x="${L + i * step}" y="${T}" width="${step}" height="${H - T - B}" fill="transparent" data-i="${i}"/>`; });
    s += `</svg><div class="analytics-tip"></div>`;
    host.innerHTML = s;
    const svg = host.querySelector("svg"), tip = host.querySelector(".analytics-tip");
    const show = e => {
      const hit = e.target.closest?.("rect[data-i]");
      if (!hit) { tip.classList.remove("on"); return; }
      const i = Number(hit.dataset.i), k = svg.getBoundingClientRect().width / W;
      tip.innerHTML = opts.tip(values[i], i);
      tip.style.left = `${Math.min(W - 60, Math.max(60, L + i * step + step / 2)) * k}px`;
      tip.style.top = `${(y(Math.max(values[i], 0)) - 6) * k}px`;
      tip.classList.add("on");
    };
    svg.addEventListener("pointermove", show);
    svg.addEventListener("click", show);
    svg.addEventListener("pointerleave", () => tip.classList.remove("on"));
  }

  function renderDays() {
    const card = $("analyticsDaysCard");
    card.classList.toggle("hidden", data.mode === "year");
    if (data.mode === "year") return;
    const days = data.days, values = days.map(d => d.amount), limit = data.daily_limit;
    const passed = days.filter(d => d.date <= data.today);
    const average = passed.length ? passed.reduce((s, d) => s + d.amount, 0) / passed.length : 0;
    const every = days.length > 20 ? 7 : days.length > 10 ? 3 : 1;
    const labels = days.map((d, i) => i % every === 0 ? String(Number(d.date.slice(8))) : "");
    $("analyticsDaysNote").textContent = limit ? `лимит ${rub(limit)}/день` : `в среднем ${rub(average)}/день`;
    barChart($("analyticsDays"), values, labels, {
      aria: "Траты по дням",
      line: limit || average,
      lineColor: limit ? "var(--expense)" : "var(--muted)",
      color: v => limit && v > limit ? "var(--expense)" : "var(--kind-daily)",
      tip: (v, i) => `<b>${day(days[i].date)}</b> · ${rub(v)}${limit && v > limit ? ` · +${rub(v - limit)}` : ""}`,
    });
    $("analyticsDaysLegend").innerHTML = limit
      ? `<span><i style="background:var(--kind-daily)"></i>В пределах лимита</span><span><i style="background:var(--expense)"></i>Перерасход</span><span><i class="line"></i>Лимит дня</span>`
      : `<span><i style="background:var(--kind-daily)"></i>Траты за день</span><span><i class="avg"></i>В среднем</span>`;
  }

  function renderMonths() {
    const months = data.months, values = months.map(m => m.daily);
    const from = data.start.slice(0, 7), to = data.end.slice(0, 7);
    barChart($("analyticsMonths"), values, months.map(m => m.label.slice(0, 3)), {
      aria: "Траты по месяцам",
      color: (v, i) => data.mode === "year" || (months[i].month >= from && months[i].month <= to) ? "var(--kind-daily)" : "color-mix(in srgb, var(--kind-daily) 40%, var(--surface-2))",
      tip: (v, i) => `<b>${esc(months[i].label)}</b> · ${rub(v)}`,
    });
    const filled = values.filter(v => v > 0);
    $("analyticsMonthsNote").textContent = filled.length
      ? `В среднем ${rub(filled.reduce((a, b) => a + b, 0) / filled.length)} в месяц${filled.length < values.length ? ` (месяцев с тратами: ${filled.length})` : ""}.`
      : "Трат за эти месяцы нет.";
  }

  function renderBills() {
    const bills = data.bills;
    const paid = bills.reduce((s, b) => s + b.paid_amount, 0), due = bills.reduce((s, b) => s + b.due_amount, 0);
    $("analyticsBillsTotal").textContent = bills.length ? `оплачено ${rub(paid)}` : "";
    $("analyticsBills").innerHTML = bills.length ? bills.map(b => {
      let status, value = b.paid_amount;
      if (!b.due_amount) status = `<small class="ok">✓ оплачено${b.count > 1 ? ` ×${b.count}` : ""}</small>`;
      else if (!b.paid_count) { status = `<small>к оплате ${day(b.next_due)}</small>`; value = b.due_amount; }
      else status = `<small>оплачено ${b.paid_count} из ${b.count} · ещё ${rub(b.due_amount)}</small>`;
      return `<div class="analytics-bill"><span>${esc(b.title)}${status}</span><span class="num">${rub(value)}</span></div>`;
    }).join("") + (due > 0 ? `<div class="analytics-bill analytics-bill-due"><span>Ещё к оплате</span><span class="num">${rub(due)}</span></div>` : "")
      : `<div class="empty">Обязательных платежей за этот период нет.</div>`;
  }

  document.querySelectorAll("[data-amode]").forEach(b => b.addEventListener("click", () => {
    view = {mode: b.dataset.amode, anchor: null};
    open.clear();
    load();
  }));
  $("analyticsPrev")?.addEventListener("click", () => { if (data) { view.anchor = data.previous_anchor; open.clear(); load(); } });
  $("analyticsNext")?.addEventListener("click", () => { if (data?.next_anchor) { view.anchor = data.next_anchor; open.clear(); load(); } });
  $("analyticsCategories")?.addEventListener("click", e => {
    const el = e.target.closest?.("[data-cat]");
    if (!el) return;
    const key = el.dataset.cat;
    if (open.has(key)) open.delete(key); else open.add(key);
    renderCategories();
  });
  document.querySelector('[data-nav="analytics"]')?.addEventListener("click", () => load());
  $("refreshBtn")?.addEventListener("click", () => {
    if (document.querySelector('[data-page="analytics"]')?.classList.contains("active")) load();
  });
  window.budgetAnalytics = {load, render: () => render(), get data() { return data; }};
})();
