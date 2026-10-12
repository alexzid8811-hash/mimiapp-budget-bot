// Home on a wide screen: two independent columns. A plain grid locks each row
// to its tallest card, so a short card next to a tall one leaves empty space
// below it. Here the grid has 1px rows and every card spans exactly its own
// height, so cards in a column stack right under each other.
(() => {
  const page = document.querySelector('.page[data-page="home"]');
  if (!page) return;
  const wide = window.matchMedia("(min-width:900px)");
  const GAP = 8;
  const fit = () => {
    const on = wide.matches;
    page.classList.toggle("masonry", on);
    for (const card of page.children) {
      card.style.gridRowEnd = on && card.offsetHeight ? `span ${Math.ceil(card.offsetHeight) + GAP}` : "";
    }
  };
  let queued = false;
  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fit(); });
  };
  const observer = new ResizeObserver(schedule);
  for (const card of page.children) observer.observe(card);
  // Cards hidden with display:none report no resize when they appear again.
  new MutationObserver(schedule).observe(page, { subtree: true, attributes: true, attributeFilter: ["class", "hidden", "style"], childList: true });
  wide.addEventListener("change", schedule);
  fit();
})();
