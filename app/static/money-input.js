(() => {
  const selector = 'input[data-money]';

  function parseMoney(value) {
    let s = String(value ?? '')
      .replace(/[\u00A0\u202F\s]/g, '')
      .replace(',', '.')
      .replace(/[^0-9.\-]/g, '');

    const firstDot = s.indexOf('.');
    if (firstDot !== -1) {
      s = s.slice(0, firstDot + 1) + s.slice(firstDot + 1).replace(/\./g, '');
    }
    const valueNumber = Number(s);
    return Number.isFinite(valueNumber) ? valueNumber : 0;
  }

  function formatEditing(value) {
    let s = String(value ?? '')
      .replace(/[\u00A0\u202F\s]/g, '')
      .replace(/\./g, ',')
      .replace(/[^0-9,]/g, '');

    if (!s) return '';

    const commaIndex = s.indexOf(',');
    const hasComma = commaIndex !== -1;
    let integerPart = hasComma ? s.slice(0, commaIndex) : s;
    let fractionPart = hasComma ? s.slice(commaIndex + 1).replace(/,/g, '').slice(0, 2) : '';

    integerPart = integerPart.replace(/^0+(?=\d)/, '') || '0';
    integerPart = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');

    return integerPart + (hasComma ? `,${fractionPart}` : '');
  }

  function formatMoney(value) {
    return new Intl.NumberFormat('ru-RU', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      useGrouping: true,
    }).format(parseMoney(value));
  }

  function normalizeForApi(input) {
    if (!input || !input.matches(selector)) return;
    input.value = String(parseMoney(input.value));
  }

  function formatField(input) {
    if (!input || !input.matches(selector) || document.activeElement === input) return;
    if (input.value === '') return;
    input.value = formatMoney(input.value);
  }

  function formatAll() {
    document.querySelectorAll(selector).forEach(formatField);
  }

  document.addEventListener('input', (event) => {
    const input = event.target;
    if (!input.matches?.(selector)) return;
    const formatted = formatEditing(input.value);
    input.value = formatted;
    try { input.setSelectionRange(formatted.length, formatted.length); } catch (_) {}
  });

  document.addEventListener('blur', (event) => {
    const input = event.target;
    if (!input.matches?.(selector) || input.value === '') return;
    input.value = formatMoney(input.value);
  }, true);

  document.addEventListener('click', (event) => {
    const button = event.target.closest?.('#savePayrollBtn, #saveSettingsBtn, #saveCashflowBtn');
    if (!button) return;
    document.querySelectorAll(selector).forEach(normalizeForApi);
    setTimeout(formatAll, 0);
  }, true);

  document.addEventListener('submit', () => {
    document.querySelectorAll(selector).forEach(normalizeForApi);
    setTimeout(formatAll, 0);
  }, true);

  setInterval(formatAll, 300);
  formatAll();

  window.ruMoneyInput = { parseMoney, formatMoney, formatEditing };

})();
