(() => {
  let timeZone = 'Europe/Moscow';
  window.budgetDate = {
    configure(zone) { timeZone = zone || 'Europe/Moscow'; },
    today(now = new Date()) {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
      }).formatToParts(now);
      const value = type => parts.find(part => part.type === type).value;
      return `${value('year')}-${value('month')}-${value('day')}`;
    },
    shift(iso, days) {
      const [year, month, day] = iso.split('-').map(Number);
      const value = new Date(Date.UTC(year, month - 1, day + days));
      return value.toISOString().slice(0, 10);
    },
  };
})();
