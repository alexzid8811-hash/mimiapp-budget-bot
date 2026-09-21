(() => {
  const $ = id => document.getElementById(id);
  const money = n => new Intl.NumberFormat("ru-RU", {style:"currency",currency:$("currency")?.value || "RUB",minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(n)||0);
  const date = s => new Intl.DateTimeFormat("ru-RU",{day:"numeric",month:"short"}).format(new Date(s+"T12:00:00"));
  const shift = (s,n) => window.budgetDate.shift(s,n);
  const days = (a,b) => Math.round((Date.parse(b+"T12:00:00Z")-Date.parse(a+"T12:00:00Z"))/86400000);
  const plural = n => n%100>=11&&n%100<=14 ? "дней" : n%10===1 ? "день" : n%10>=2&&n%10<=4 ? "дня" : "дней";
  let currentPage = "home";
  function navigation(page) {
    currentPage = page;
    const titles={home:"Мой бюджет",operations:"Операции",plan:"Платежи",buffer:"Резервы",piggy:"Резервы",settings:"Настройки"};
    $("hello").textContent=titles[page] || "Мой бюджет";
    document.body.classList.toggle("wide",page==="buffer");
    document.querySelectorAll("[data-nav]").forEach(b=>{
      if(b.dataset.nav===(page==="piggy"?"buffer":page)) b.setAttribute("aria-current","page");
      else b.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-seg]").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.seg===page)));
  }
  function payroll() {
    $("payDaysBadge").textContent=`${$("salaryDay").value || "?"} и ${$("advanceDay").value || "?"} числа`;
    // Saving remains accessible even when the automatic calculation is switched off.
    $("payrollFields").classList.toggle("payroll-off",!$("payrollEnabled").checked);
  }
  function render(state) {
    const d=state.dashboard;
    if(!d) return;
    const flow=state.cashflow?.enabled ? state.cashflow : null;
    const spent=Number(d.spent_today)||0;
    const left=Number(flow ? flow.available_today : d.daily_available)||0;
    const target=flow ? Number(flow.today_target ?? left+spent) : left+spent;
    // The daily limit is always the full amount available at the start of today.
    // Overspending is shown separately in the remaining amount.
    const headline=left<0 ? left : target;
    $("dailyAvailable").textContent=money(headline);
    $("dailyAvailable").style.color=left<0?"var(--expense)":"";
    $("leftToday").textContent=money(left);
    $("leftToday").style.color=left<0?"var(--expense)":"";
    const pct=target>0 ? Math.min(100,Math.max(0,spent/target*100)) : spent>0?100:0;
    $("spendFill").style.width=pct+"%";
    $("spendFill").style.background=left<0?"var(--expense)":"var(--accent)";
    $("spendBar").setAttribute("aria-valuenow",String(Math.round(pct)));
    const start=d.period.start, next=shift(d.period.end,1), today=flow?.today || window.budgetDate.today();
    const count=days(today,next);
    const nextRow=flow?.periods?.find(p=>p.start===next);
    const kind=nextRow?.kind || "выплата";
    $("nextPayTitle").textContent=kind==="Аванс" ? `До аванса ${count} ${plural(count)}` : `До выплаты ${count} ${plural(count)}`;
    $("nextPayDate").textContent=date(next);
    $("periodStartLabel").textContent=date(start);
    $("periodEndLabel").textContent=date(next)+" · "+kind.toLowerCase();
    const strip=$("periodStrip");strip.replaceChildren();
    const bills=new Set((state.plan||[]).map(p=>p.due_date));
    for(let i=0;i<=Math.min(62,days(start,next));i++){
      const s=shift(start,i), el=document.createElement("div");
      el.className="day "+(s===next?"payday":s<today?"past":s===today?"today":"")+(bills.has(s)?" bill":"");
      el.title=date(s)+(bills.has(s)?" · обязательный платёж":"");
      strip.appendChild(el);
    }
    const periods=flow?.periods||[];
    const reservedNow=$("reservedNow");
    if(reservedNow) reservedNow.textContent=money(flow ? periods[0]?.put_aside || 0 : Math.max(0,Number(d.reserve.auto_movement)||0));
    const balance=Number(flow ? flow.buffer_balance : d.reserve.balance)||0;
    const peak=flow ? Math.max(balance,...periods.map(p=>Number(p.buffer)||0)) : Number(d.reserve.future_target)||0;
    $("bufferMeter").style.width=(peak>0?Math.min(100,Math.max(0,balance/peak*100)):0)+"%";
    payroll();navigation(currentPage);
  }
  document.addEventListener("click",event=>{
    const go=event.target.closest("[data-seg],[data-go]");
    if(go) switchPage(go.dataset.seg || go.dataset.go);
    const close=event.target.closest("[data-close]");
    if(close) close.closest("dialog").close();
  });
  document.querySelectorAll("dialog").forEach(d=>d.addEventListener("click",e=>{
    if(e.target!==d) return;
    const r=d.getBoundingClientRect();
    if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) d.close();
  }));
  $("themeBtn").addEventListener("click",()=>{
    window.budgetTheme?.apply(document.documentElement.dataset.theme==="dark"?"light":"dark",true);
  });
  ["salaryDay","advanceDay","payrollEnabled"].forEach(id=>$(id).addEventListener("input",payroll));
  window.budgetDesign={render,navigation};
})();
