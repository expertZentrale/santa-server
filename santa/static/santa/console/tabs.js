// The tab bars (.tabs: the sections of the administration, the views of the events). On a narrow screen they scroll
// sideways: the active tab is scrolled into view when its page loads (a click on a tab further right loads a page
// that started at the left), and arrows at the edges show that there is more and scroll there.
(function () {
  "use strict";

  function centre(tabs, tab) {
    const bar = tabs.getBoundingClientRect();
    const box = tab.getBoundingClientRect();
    tabs.scrollLeft += box.left - bar.left - (bar.width - box.width) / 2;
  }

  function setUp(tabs) {
    if (tabs.dataset.tabsReady) return;
    tabs.dataset.tabsReady = "1";
    const scroller = document.createElement("div");
    scroller.className = "tabs-scroller";
    tabs.before(scroller);
    scroller.append(tabs);
    const arrows = document.getElementById("tabs-arrows");
    if (arrows) scroller.append(arrows.content.cloneNode(true));
    const update = () => {
      const max = tabs.scrollWidth - tabs.clientWidth;
      scroller.classList.toggle("more-left", tabs.scrollLeft > 1);
      scroller.classList.toggle("more-right", tabs.scrollLeft < max - 1);
    };
    const active = tabs.querySelector('.active, [aria-selected="true"], [aria-current="page"]');
    if (active) centre(tabs, active);
    tabs.addEventListener("scroll", update, { passive: true });
    if (window.ResizeObserver) new ResizeObserver(update).observe(tabs);
    scroller.addEventListener("click", (event) => {
      const arrow = event.target.closest("[data-tabs-scroll]");
      if (!arrow) return;
      const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      tabs.scrollBy({ left: Number(arrow.dataset.tabsScroll) * tabs.clientWidth * 0.7,
                      behavior: smooth ? "smooth" : "auto" });
    });
    update();
  }

  SantaConsole.onInit((root) => root.querySelectorAll?.(".tabs").forEach(setUp));
  SantaConsole.onSwap((target) => target.querySelectorAll?.(".tabs").forEach(setUp));
})();
