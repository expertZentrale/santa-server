// The filter bar of the lists (widgets/_filter_bar.html) and the checkbox pickers (details[data-picker])
(function () {
  "use strict";

  // a chip applies its checkboxes when it closes, a preset or choice right away, a date range with "Apply";
  // "+ Filter" shows the chip of a filter and opens it
  function setupFilterBar(form) {
    if (form.dataset.filterReady) return;
    form.dataset.filterReady = "1";
    const submit = () => (form.requestSubmit ? form.requestSubmit() : form.submit());
    // Enter in the search: the default button of the form can be the "Apply" of a hidden time filter, disabled
    form.querySelector('input[type="search"]')?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;
      event.preventDefault();
      submit();
    });
    form.querySelectorAll("details[data-chip]").forEach((chip) => {
      let changed = false;
      chip.addEventListener("change", (event) => {
        if (event.target.type === "radio") {
          // "is / is not" of a lookup: right away only with values chosen, else with them when the chip closes
          if (event.target.hasAttribute("data-lookup-mode")) {
            if (chip.querySelector('input[type="checkbox"]:checked')) submit();
            else changed = true;
            return;
          }
          // the date range waits for "Apply"
          if (!event.target.hasAttribute("data-range")) submit();
          return;
        }
        if (event.target.type === "checkbox") changed = true;
      });
      // closed without a change, a chip added with "+ Filter" stays ("all"); it goes with the next search
      chip.addEventListener("toggle", () => {
        // a lookup (Mac, user) lists its first ones when it opens, not only once its search is used
        const search = chip.querySelector("[data-lookup-search]");
        if (chip.open && search && window.htmx) {
          htmx.trigger(search, "lookup-open");
          search.focus();
        }
        if (!chip.open && changed) submit();
      });
    });
    // renaming a view: the field gets the focus; Cancel closes it with the saved name, back to the pencil
    form.querySelectorAll("details.rename-view").forEach((rename) => {
      const input = rename.querySelector("input");
      rename.addEventListener("toggle", () => {
        if (rename.open) input.focus();
        else input.value = input.defaultValue;
      });
      rename.querySelector("[data-rename-cancel]").addEventListener("click", () => {
        rename.open = false;
        rename.querySelector("summary").focus();
      });
    });
    form.addEventListener("click", (event) => {
      const item = event.target.closest("[data-open-facet]");
      if (!item) return;
      item.closest("details").open = false;
      // after the click: the document closes the pickers the click was not in
      requestAnimationFrame(() => openFacet(form, item.dataset.openFacet));
    });
  }

  // the chip of a filter shown (also one not chosen yet, as "+ Filter" adds it) and opened
  function openFacet(form, name, focus = true) {
    const chip = form.querySelector(`.filter-chip[data-facet="${name}"]`);
    const details = chip?.querySelector("details");
    if (!details) return;
    const item = form.querySelector(`[data-open-facet="${name}"]`);
    if (item) item.hidden = true;
    chip.hidden = false;
    chip.querySelector("fieldset")?.removeAttribute("disabled");
    details.open = true;
    if (focus) (details.querySelector("[data-lookup-search]") || details.querySelector("input"))?.focus();
  }

  // A filter applies with the page loaded again in place (a boosted form): the bar is new, and a menu opened while
  // it loaded ("+ Filter" right after a chip) closed with it. Open again: the menus of the bar and the column chooser
  // that were open, on the same list.
  const MENUS = "[data-filter-form] details[open], .columns-picker[open]";
  let reopen = null;

  function menuKey(details) {
    if (details.matches(".filter-add")) return "add";
    if (details.matches(".views-picker")) return "views";
    if (details.matches(".columns-picker")) return "columns";
    const chip = details.closest(".filter-chip");
    return chip ? `chip:${chip.dataset.facet}` : null;
  }

  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target !== document.body || !event.detail.shouldSwap) return;
    const path = event.detail.pathInfo?.finalRequestPath || event.detail.pathInfo?.requestPath || "";
    const samePage = new URL(path, window.location.href).pathname === window.location.pathname;
    const keys = [...document.querySelectorAll(MENUS)].map(menuKey).filter(Boolean);
    const focused = document.activeElement?.closest?.("details[open]");
    reopen = samePage && keys.length ? { keys, focus: focused ? menuKey(focused) : null } : null;
  });

  SantaConsole.onInit((root) => {
    if (!reopen || root !== document.body) return;
    const { keys, focus } = reopen;
    reopen = null;
    // after the other scripts set up the page (table.js adds the column chooser)
    requestAnimationFrame(() => {
      const form = document.querySelector("[data-filter-form]");
      keys.forEach((key) => {
        if (key.startsWith("chip:")) {
          if (form) openFacet(form, key.slice(5), key === focus);
          return;
        }
        // the column chooser is in the filter row or above the table (table.js)
        const selector = { add: "[data-filter-form] .filter-add", views: "[data-filter-form] .views-picker",
                           columns: "main .columns-picker:not([data-placeholder])" }[key];
        const open = () => {
          const details = document.querySelector(selector);
          if (!details) return false;
          details.open = true;
          if (key === focus) details.querySelector("summary")?.focus();
          return true;
        };
        if (open() || key !== "columns") return;
        // the rows of the list come after the page, the column chooser with them
        const later = (event) => {
          if (!event.detail.target?.matches?.("[data-list-rows]")) return;
          document.removeEventListener("htmx:afterSwap", later);
          requestAnimationFrame(open);
        };
        document.addEventListener("htmx:afterSwap", later);
      });
    });
  });

  SantaConsole.onInit((root) => root.querySelectorAll("[data-filter-form]").forEach(setupFilterBar));

  // the checkbox pickers: the count on the button, closed by a click outside or Escape
  document.addEventListener("change", (event) => {
    const picker = event.target.closest("[data-picker]");
    if (!picker) return;
    const count = picker.querySelectorAll("input:checked").length;
    const badge = picker.querySelector("[data-picker-count]");
    if (!badge) return;
    badge.textContent = count;
    badge.hidden = !count;
  });
  document.addEventListener("click", (event) => {
    document.querySelectorAll("[data-picker][open]").forEach((picker) => {
      if (!picker.contains(event.target)) picker.open = false;
    });
  });
  document.addEventListener("keydown", (event) => {
    const open = event.key === "Escape" && document.querySelector("[data-picker][open]");
    if (!open) return;
    // only the picker closes, not the drawer or the selection (loaded before drawer.js and table.js)
    event.stopImmediatePropagation();
    open.open = false;
    open.querySelector("summary").focus();
  });
  // Phones: the menus of the bar are as wide as the window (filterbar.css, position: fixed). Without a top, a fixed
  // box stays where it would be in the page: scrolled down, it opened that far below its button
  const narrow = window.matchMedia("(max-width: 40em)");
  const BAR_MENUS = ".filter-bar .picker.down[open] > .picker-panel, .filter-tools .picker.down[open] > .picker-panel";

  function place(panel) {
    if (!narrow.matches) {
      panel.style.top = "";
      panel.style.maxHeight = "";
      return;
    }
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    // the gap below the button is the margin of the panel
    const top = panel.parentElement.querySelector(":scope > summary").getBoundingClientRect().bottom;
    panel.style.top = `${top / rem}rem`;
    // at most 60% of the window, and never below its bottom
    panel.style.maxHeight = `${Math.max(8 * rem, Math.min(0.6 * window.innerHeight, window.innerHeight - top - 1.5 * rem))
      / rem}rem`;
  }

  function placeMenus() {
    document.querySelectorAll(BAR_MENUS).forEach(place);
  }

  // placed before it opens (the click on its button, also Enter / Space): placed only when the "toggle" event came,
  // it showed for a moment at the wrong place first
  document.addEventListener("click", (event) => {
    const summary = event.target.closest?.(".filter-bar .picker.down > summary, .filter-tools .picker.down > summary");
    const panel = summary?.parentElement.querySelector(":scope > .picker-panel");
    if (panel && !summary.parentElement.open) place(panel);
  }, true);
  // opened otherwise (by a script): as soon as it is open. toggle doesn't bubble: caught on the way down
  document.addEventListener("toggle", (event) => {
    if (event.target.matches?.(".filter-bar .picker.down, .filter-tools .picker.down")) placeMenus();
  }, true);
  let frame = 0;
  const later = () => {
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; placeMenus(); });
  };
  window.addEventListener("scroll", later, { passive: true });
  window.addEventListener("resize", later);
})();
