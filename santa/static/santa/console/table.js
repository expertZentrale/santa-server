// The tables of the lists: selection and bulk bar, column widths and chooser, labels for phones, keyboard
(function () {
  "use strict";

  const { rem, columns } = SantaConsole;

  // Selection and bulk bar of the lists: the bar floats at the bottom, the table never moves
  function rowBoxes(form) {
    return [...form.querySelectorAll('tbody input[type="checkbox"]')];
  }

  // Phones show the rows as cards without the header: "select all" goes above them
  function addSelectAllCards(root) {
    const text = document.getElementById("table-texts")?.dataset.selectAll;
    root.querySelectorAll("[data-select-form] thead [data-select-all]").forEach((all) => {
      const scroll = all.closest(".table-scroll") || all.closest("table");
      if (!text || scroll.previousElementSibling?.matches(".select-all-cards")) return;
      const label = document.createElement("label");
      label.className = "select-all-cards";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.id = `${all.id || "select-all"}-cards`;
      box.dataset.selectAll = "";
      label.append(box, text);
      scroll.before(label);
    });
  }

  function updateSelection(form) {
    const boxes = rowBoxes(form);
    boxes.forEach((box) => box.closest("tr")?.classList.toggle("selected", box.checked));
    const checked = boxes.filter((box) => box.checked).length;
    const bar = form.querySelector("[data-bulkbar]");
    if (bar) {
      // the hidden attribute only avoids a flash before this script runs
      bar.hidden = false;
      bar.classList.toggle("visible", checked > 0);
      bar.querySelector("[data-selected-count]").textContent = checked;
    }
    // the header checkbox, and its copy above the cards on phones
    form.querySelectorAll("[data-select-all]").forEach((all) => {
      all.checked = checked > 0 && checked === boxes.length;
      all.indeterminate = checked > 0 && checked < boxes.length;
    });
    document.body.classList.toggle("has-bulkbar", !!document.querySelector("[data-bulkbar].visible"));
    reserveBarSpace();
  }

  // the room below the last row follows the height of the bar (main padding in table.css)
  function reserveBarSpace() {
    const bar = document.querySelector("[data-bulkbar].visible");
    if (bar) document.body.style.setProperty("--bulkbar-h", `${bar.offsetHeight / rem()}rem`);
  }

  // the bar changes its height after a selection too: fields shown for an action (forms.js), wrapping on a
  // rotated phone or a resized window
  const barObserver = "ResizeObserver" in window ? new ResizeObserver(reserveBarSpace) : null;

  function updateAllSelections() {
    document.querySelectorAll("[data-select-form]").forEach(updateSelection);
  }

  function clearSelection(form) {
    rowBoxes(form).forEach((box) => {
      box.checked = false;
    });
    updateSelection(form);
  }

  // the user changed the selection of a list (forms.js: "create rules" follows it)
  function selectionChanged(form) {
    updateSelection(form);
    form.dispatchEvent(new CustomEvent("santa:selection", { bubbles: true }));
  }

  // Column names on every cell: on phones the rows become cards with "label: value"
  function labelTables(root) {
    const tables = root.matches?.("table.table") ? [root] : root.querySelectorAll("table.table");
    tables.forEach((table) => {
      if (!table.parentElement.classList.contains("table-scroll")) {
        const wrapper = document.createElement("div");
        wrapper.className = "table-scroll";
        table.before(wrapper);
        wrapper.append(table);
      }
      // the rows that came after the layout was set (a page, live updates, a row swapped after its switch)
      columns.layoutRows(table);
      if (table._layout) {
        // by column: the order is the user's, merged columns name both ("Identifier / Comment")
        table.querySelectorAll("tbody tr").forEach((row) => {
          [...row.children].forEach((cell) => {
            const key = cell.dataset.col;
            if (key) cell.setAttribute("data-label", columns.groupLabel(table, key, table._hidden));
            else if (!cell.hasAttribute("data-label")) cell.setAttribute("data-label", "");
          });
        });
        // the entry link in the first column shown, wherever the main column is now
        columns.placeRowLinks(table, table._hidden);
        return;
      }
      const labels = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
      table.querySelectorAll("tbody tr").forEach((row) => {
        [...row.children].forEach((cell, index) => {
          if (!cell.hasAttribute("data-label")) cell.setAttribute("data-label", labels[index] || "");
        });
      });
    });
  }

  // Tables with data-table="<name>": column widths by dragging (or ←/→ on the handle), a column chooser, a sticky
  // header when the table fits the page. Stored per browser; without storage the tables look as before.
  function tableSettings(table) {
    try {
      return JSON.parse(localStorage.getItem(`santa.table.${table.dataset.table}`)) || {};
    } catch (error) {
      return {};
    }
  }

  function saveTableSettings(table, settings) {
    try {
      localStorage.setItem(`santa.table.${table.dataset.table}`, JSON.stringify(settings));
    } catch (error) {
      // private window or blocked storage: the change lasts until the next page
    }
  }

  // The column widths a list had when it was shown last (this session): the grey rows of the next load take them, so
  // the columns don't move when the rows arrive. Only on wide screens: phones show cards.
  function rememberLayout(table) {
    if (cardLayout.matches || !table.dataset.table) return;
    const cells = [...table.tHead.rows[0].cells];
    if (!cells.some((th) => th.getClientRects().length)) return;
    const rows = [...(table.tBodies[0]?.rows || [])].filter((row) => !row.matches(".empty"));
    const layout = { table: table.getBoundingClientRect().width / rem(),
                     widths: cells.map((th) => (th.getClientRects().length ? th.getBoundingClientRect().width / rem() : 0)),
                     // the grey rows of the next load take their place: as many, as high, and the pages below
                     rows: rows.length,
                     rowHeight: rows.length ? table.tBodies[0].getBoundingClientRect().height / rows.length / rem() : 0,
                     pages: !!table.closest("[data-list-rows]")?.querySelector(".pagination:not([data-pages-placeholder])") };
    try {
      sessionStorage.setItem(`santa.layout.${table.dataset.table}`, JSON.stringify(layout));
    } catch (error) {
      // private window or blocked storage: the grey rows have their own widths
    }
  }

  // the grey rows as the rows were last time (rememberLayout): their number and height, the pages bar if there was one
  function skeletonLikeLast(table, layout) {
    const body = table.tBodies[0];
    const skeletons = [...(body?.rows || [])].filter((row) => row.matches(".skeleton"));
    if (!skeletons.length || !layout.rows) return;
    // never more than the page size of this load (the server renders that many)
    const count = Math.min(layout.rows, skeletons.length);
    while (body.querySelectorAll("tr.skeleton").length > count) body.querySelector("tr.skeleton:last-child").remove();
    while (body.querySelectorAll("tr.skeleton").length < count) body.append(skeletons[0].cloneNode(true));
    if (layout.rowHeight) {
      body.querySelectorAll("tr.skeleton > td").forEach((td) => { td.style.height = `${layout.rowHeight}rem`; });
    }
    if (layout.pages) table.closest("[data-list-rows]")?.querySelector("[data-pages-placeholder]")?.removeAttribute("hidden");
  }

  function applyRememberedLayout(table) {
    if (cardLayout.matches) return false;
    let layout = null;
    try {
      layout = JSON.parse(sessionStorage.getItem(`santa.layout.${table.dataset.table}`));
    } catch (error) {
      return false;
    }
    const cells = [...table.tHead.rows[0].cells];
    if (!layout || layout.widths?.length !== cells.length) return false;
    cells.forEach((th, index) => { th.style.width = `${layout.widths[index]}rem`; });
    table.classList.add("fixed");
    table.style.width = `${layout.table}rem`;
    skeletonLikeLast(table, layout);
    return true;
  }

  function tableColumns(table) {
    return [...table.tHead.rows[0].cells].map((th, index) => ({ th, index, key: th.dataset.col }))
      .filter((column) => column.key);
  }

  function applyWidths(table, settings) {
    const widths = settings.widths || {};
    const cells = [...table.tHead.rows[0].cells];
    // start from the natural layout, so a column shown again gets its own width (hidden, it measures 0)
    table.classList.remove("fixed");
    table.style.width = "";
    cells.forEach((th) => { th.style.width = ""; });
    if (!Object.keys(widths).length || !tableColumns(table).length) return;
    // fixed layout: the visible columns keep their natural width, unless one was set; hidden ones get none
    const visible = cells.filter((th) => th.getClientRects().length);
    // phones: the header is hidden (cards), there is nothing to measure until it is back
    if (!visible.length) return;
    const natural = visible.map((th) => th.getBoundingClientRect().width / rem());
    visible.forEach((th, index) => {
      const key = th.dataset.col;
      th.style.width = `${(key && widths[key]) || natural[index]}rem`;
    });
    table.classList.add("fixed");
    table.style.width = `${visible.reduce((sum, th) => sum + parseFloat(th.style.width), 0)}rem`;
  }

  // optional columns added after a browser stored its choice (it knew the others): hidden there too, as by default
  const LATER_OPTIONAL = new Set(["expires", "name"]);

  function hiddenColumns(table, settings) {
    const optional = (table.dataset.optional || "").split(" ").filter(Boolean);
    if (!settings.hidden) return new Set(optional);
    const known = settings.known ? new Set(settings.known) : null;
    let hidden = settings.hidden;
    // stored before columns could be merged: the comment, path, group and Team ID were hidden as columns of their
    // own but shown under another one; merged by default now, they stay shown there
    if (!settings.merged) hidden = hidden.filter((key) => !columns.defaultLayout(table).merged[key]);
    return new Set([...hidden, ...optional.filter((key) => (known ? !known.has(key) : LATER_OPTIONAL.has(key)))]);
  }

  // the order and merges of the user (or of a view), else the ones of the list. A column added to the list after
  // they were stored gets its default merge (e.g. the e-mail under the user), not a place of its own.
  function storedLayout(table, settings) {
    if (!settings.order) return columns.defaultLayout(table);
    const merged = { ...(settings.merged || {}) };
    const known = new Set(settings.known || settings.order);
    Object.entries(columns.defaultLayout(table).merged).forEach(([child, parent]) => {
      if (!known.has(child) && !(child in merged)) merged[child] = parent;
    });
    return { order: settings.order, merged };
  }

  function applyHidden(table, settings) {
    const hidden = hiddenColumns(table, settings);
    table._hidden = hidden;
    let style = document.getElementById(`table-style-${table.dataset.table}`);
    if (!style) {
      style = document.createElement("style");
      style.id = `table-style-${table.dataset.table}`;
      document.head.append(style);
    }
    const selector = `table[data-table="${table.dataset.table}"]`;
    // by key: also for rows that come later (live updates, toggles), wherever the column is now
    style.textContent = [...hidden].filter((key) => /^[a-z0-9-]+$/.test(key))
      .map((key) => `${selector} tr > [data-col="${key}"], ${selector} .merged-part[data-col="${key}"], `
                    + `${selector} .merged-label[data-col="${key}"] { display: none; }`).join("\n");
    table.dataset.shown = columns.keys(table).filter((key) => !hidden.has(key)).join(" ");
    // the row "nothing found" and the grey rows span the columns shown: spanning the hidden ones too made the table
    // wider than its header
    const shown = [...table.tHead.rows[0].cells].filter((th) => !hidden.has(th.dataset.col)).length;
    table.querySelectorAll(":scope > tbody > tr.empty > td").forEach((td) => { td.colSpan = shown; });
    table.querySelectorAll(":scope > tbody > tr.skeleton").forEach((row) => {
      while (row.cells.length > shown) row.lastElementChild.remove();
    });
    return hidden;
  }

  function updateFits(table) {
    const box = table.closest(".table-scroll");
    if (!box) return;
    box.classList.remove("fits");
    box.classList.toggle("fits", box.scrollWidth <= box.clientWidth + 1);
    // the width of the pages first: their height (--pages-h) follows from it
    alignPages(table, box);
    measurePages();
    updateScrollbar(box);
    moveHeader(table);
  }

  // the pages are as wide as a table that grows out of the page, like its scrollbar (not the cards on phones)
  function alignPages(table, box) {
    const pages = table.closest("[data-list-rows]")?.querySelector(".pagination");
    if (!pages) return;
    const wide = !cardLayout.matches && box.offsetWidth > pages.parentElement.clientWidth + 1;
    pages.classList.toggle("wide", wide);
    if (wide) pages.style.setProperty("--table-w", `${box.offsetWidth / rem()}rem`);
    else pages.style.removeProperty("--table-w");
  }

  // A table wider than the page scrolls sideways in its box, the page alone scrolls down: the box's scrollbar is at
  // the bottom of the window while the table is on screen (a box scrolling down too was a second vertical one)
  function addScrollbar(box) {
    if (box.nextElementSibling?.matches(".table-hscroll")) return;
    const bar = document.createElement("div");
    bar.className = "table-hscroll";
    bar.setAttribute("aria-hidden", "true");
    bar.hidden = true;
    bar.append(document.createElement("div"));
    box.after(bar);
    box.classList.add("has-hscroll");
    // its own scrollbar is hidden: the keyboard scrolls it
    if (!box.hasAttribute("tabindex")) box.tabIndex = 0;
    // one follows the other; the position set is the one it reports back, which ends it
    bar.addEventListener("scroll", () => {
      if (box.scrollLeft !== bar.scrollLeft) box.scrollLeft = bar.scrollLeft;
    }, { passive: true });
    box.addEventListener("scroll", () => {
      if (bar.scrollLeft !== box.scrollLeft) bar.scrollLeft = box.scrollLeft;
    }, { passive: true });
  }

  // the pages stay at the bottom of the window (sticky, not on phones): the scrollbar of a wide table and the bulk
  // actions sit on them (table.css)
  function measurePages() {
    const pages = document.querySelector("main .pagination");
    const sticky = pages && getComputedStyle(pages).position === "sticky";
    document.body.style.setProperty("--pages-h", `${sticky ? pages.offsetHeight / rem() : 0}rem`);
  }

  function updateScrollbar(box) {
    const bar = box.nextElementSibling;
    if (!bar?.matches(".table-hscroll")) return;
    bar.hidden = box.classList.contains("fits") || cardLayout.matches;
    if (bar.hidden) return;
    bar.style.width = `${box.offsetWidth / rem()}rem`;
    bar.firstElementChild.style.width = `${box.scrollWidth / rem()}rem`;
    bar.scrollLeft = box.scrollLeft;
  }

  // the header of a wide table under the top bar: CSS can't (sticky stops at the box that scrolls sideways)
  function moveHeader(table) {
    const box = table.closest(".table-scroll");
    const cells = table.tHead ? [...table.tHead.rows[0].cells] : [];
    if (!box || !cells.length) return;
    let offset = 0;
    if (!box.classList.contains("fits") && !cardLayout.matches && !table.classList.contains("compact")) {
      const topbar = document.querySelector(".topbar")?.getBoundingClientRect().bottom || 0;
      const rect = table.getBoundingClientRect();
      const head = table.tHead.getBoundingClientRect().height;
      const lastRow = table.tBodies[0]?.rows[table.tBodies[0].rows.length - 1]?.getBoundingClientRect().height || 0;
      offset = Math.max(0, Math.min(topbar - rect.top, rect.height - head - lastRow));
    }
    const value = offset ? `translateY(${offset / rem()}rem)` : "";
    if (cells[0].style.transform !== value) cells.forEach((th) => { th.style.transform = value; });
  }

  let headerFrame = 0;
  window.addEventListener("scroll", () => {
    if (headerFrame) return;
    headerFrame = requestAnimationFrame(() => {
      headerFrame = 0;
      document.querySelectorAll("main .table-scroll:not(.fits) > table[data-table]").forEach(moveHeader);
    });
  }, { passive: true });

  // the width below which the tables are cards (table.css)
  const cardLayout = window.matchMedia("(max-width: 45em)");

  // the column chooser shows at most this many columns without scrolling (the rules have 9)
  const MAX_COLUMN_ROWS = 9;

  function smallButton(text, label, data, disabled) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "icon-button small";
    button.textContent = text;
    button.title = label;
    button.setAttribute("aria-label", label);
    Object.assign(button.dataset, data);
    button.disabled = disabled;
    return button;
  }

  // The entries of the column chooser, in the order of the table: shown or not, one step up / down, and under which
  // column it is shown (the same as dragging the headers, for keyboards and touch screens)
  function columnOptions(table, texts) {
    const layout = table._layout;
    const hidden = table._hidden || new Set();
    const tops = columns.tops(layout);
    return layout.order.map((key) => {
      const parent = layout.merged[key] || "";
      const siblings = parent ? columns.children(layout, parent) : tops;
      const index = siblings.indexOf(key);
      const name = columns.labelOf(table, key);
      const option = document.createElement("div");
      option.className = parent ? "column-option merged" : "column-option";
      option.dataset.columnOption = key;
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      // not sent with any form: an id, so the browser knows the field
      box.id = `columns-${table.dataset.table || "table"}-${key}`;
      box.checked = !hidden.has(key);
      box.dataset.columnKey = key;
      const text = document.createElement("span");
      text.className = "column-name";
      text.textContent = name;
      text.title = name;
      label.append(box, text);
      const select = document.createElement("select");
      select.dataset.columnParent = "";
      select.setAttribute("aria-label", `${texts.showBelow || "Show below"}: ${name}`);
      select.title = texts.showBelow || "Show below";
      select.append(new Option(texts.ownColumn || "Own column", ""));
      tops.filter((top) => top !== key).forEach((top) => {
        select.append(new Option(`${texts.below || "Under"} ${columns.labelOf(table, top)}`, top, false, top === parent));
      });
      option.append(label,
                    smallButton("↑", `${texts.moveUp || "Move up"}: ${name}`, { columnMove: "-1" }, index === 0),
                    smallButton("↓", `${texts.moveDown || "Move down"}: ${name}`, { columnMove: "1" },
                                index === siblings.length - 1),
                    select);
      return option;
    });
  }

  // The saved views keep the columns too (the filter bar's view-layout): a view chosen in the views, or one that is
  // not the one shown last, brings its columns; "Standard" after a view the list's own. Changed after that, the
  // columns stay so in this browser until the view is saved again.
  function syncView(table, settings) {
    if (table.closest("#drawer")) return;
    const element = document.getElementById("view-layout");
    if (!element) return;
    let data = null;
    let chosen = null;
    try {
      data = JSON.parse(element.textContent);
      chosen = sessionStorage.getItem("santa.view-chosen");
      sessionStorage.removeItem("santa.view-chosen");
    } catch (error) {
      if (!data) return;
    }
    const current = settings.view ?? null;
    if (data.id) {
      if (data.id === current && chosen !== String(data.id)) return;
      settings.view = data.id;
      // a view saved before the columns were part of it keeps the columns as they are
      const layout = data.layout || {};
      if (Array.isArray(layout.order)) {
        settings.order = layout.order;
        settings.merged = layout.merged || {};
        settings.hidden = layout.hidden || [];
        settings.known = layout.known || layout.order;
      }
      saveTableSettings(table, settings);
    } else if (data.standard && (current || chosen === "standard")) {
      ["order", "merged", "hidden", "known", "view"].forEach((name) => delete settings[name]);
      saveTableSettings(table, settings);
    }
  }

  // the columns of the list as they are now, for "Save current view"
  function currentLayout(table) {
    const layout = table._layout || columns.defaultLayout(table);
    return { order: layout.order, merged: layout.merged, hidden: [...(table._hidden || [])],
             known: columns.keys(table) };
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("[data-view-choice]");
    if (!link) return;
    try {
      sessionStorage.setItem("santa.view-chosen", link.dataset.viewChoice);
    } catch (error) {
      // blocked storage: the view brings its columns when it isn't the one shown last
    }
  });

  // before htmx sends the form (capture: its own listener is on the form)
  document.addEventListener("submit", (event) => {
    const field = event.target.querySelector?.('input[name="layout"][data-view-layout-field]');
    if (!field) return;
    const table = [...document.querySelectorAll("main table[data-table][data-table-ready]")]
      .find((candidate) => !candidate.closest("#drawer, .rows-loading"));
    field.value = table ? JSON.stringify(currentLayout(table)) : "";
  }, true);

  function setupTable(table) {
    if (table.dataset.tableReady || !table.tHead) return;
    table.dataset.tableReady = "1";
    const texts = document.getElementById("table-texts")?.dataset || {};
    const settings = tableSettings(table);
    // the keys of the columns, before anything is moved
    columns.markup(table);
    syncView(table, settings);
    if (table.closest(".rows-loading")) {
      // the grey rows while the list loads: the columns as shown last (or as the user set them), so the real table
      // takes the same place
      columns.apply(table, storedLayout(table, settings), texts);
      applyHidden(table, settings);
      if (!applyRememberedLayout(table)) applyWidths(table, settings);
      return;
    }
    // every column gets its handle, also the ones merged into another now: they keep it when split again
    columns.markup(table).filter((column) => column.key).forEach(({ th, key }) => {
      const handle = document.createElement("span");
      handle.className = "col-resize";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `${texts.resize || "Resize column"}: ${th.textContent.trim()}`);
      th.append(handle);
      const setWidth = (width) => {
        // rounded up: a cell a fraction of a pixel too narrow shows "…"
        settings.widths = { ...(settings.widths || {}), [key]: Math.max(3, Math.ceil(width * 10) / 10) };
        applyWidths(table, settings);
        updateFits(table);
      };
      // as wide as the widest cell needs (the paths without their ellipsis), at most the window
      const fit = () => {
        table.classList.remove("fixed");
        table.classList.add("measuring");
        table.style.width = "max-content";
        [...table.tHead.rows[0].cells].forEach((cell) => { cell.style.width = ""; });
        // one pixel to spare for the subpixel widths of the text
        const width = (th.getBoundingClientRect().width + 1) / rem();
        table.classList.remove("measuring");
        setWidth(Math.min(width, window.innerWidth / rem() - 4));
        saveTableSettings(table, settings);
      };
      handle.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); });
      handle.addEventListener("dblclick", fit);
      handle.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          const step = (event.shiftKey ? 2 : 0.5) * (event.key === "ArrowLeft" ? -1 : 1);
          setWidth(th.getBoundingClientRect().width / rem() + step);
          saveTableSettings(table, settings);
        } else if (event.key === "Enter") {
          event.preventDefault();
          fit();
        }
      });
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        const startX = event.clientX;
        const start = th.getBoundingClientRect().width / rem();
        const move = (moveEvent) => setWidth(start + (moveEvent.clientX - startX) / rem());
        const up = () => {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", up);
          saveTableSettings(table, settings);
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
      });
    });
    columns.apply(table, storedLayout(table, settings), texts);
    applyHidden(table, settings);
    // labelled before the layout (onInit): again, by column
    labelTables(table);
    applyWidths(table, settings);
    // phone <-> desktop (a resized window, a rotated tablet): measured again in the new layout
    cardLayout.addEventListener("change", () => {
      if (!table.isConnected) return;
      applyWidths(table, settings);
      updateFits(table);
    });

    // the column chooser: in the filter row, or above the table
    const picker = document.createElement("details");
    picker.className = "picker down columns-picker";
    picker.dataset.picker = "";
    const summary = document.createElement("summary");
    summary.className = "button";
    summary.textContent = texts.columns || "Columns";
    const panel = document.createElement("div");
    panel.className = "picker-panel";
    // the list scrolls, the reset stays at the bottom
    const list = document.createElement("div");
    list.className = "picker-list";
    const renderList = () => list.replaceChildren(...columnOptions(table, texts));
    renderList();
    const footer = document.createElement("div");
    footer.className = "picker-footer";
    const resetAll = document.createElement("button");
    resetAll.type = "button";
    resetAll.className = "menu-item";
    resetAll.textContent = texts.reset || "Reset widths and columns";
    footer.append(resetAll);
    panel.append(list, footer);
    // The same limit on every list (MAX_COLUMN_ROWS), and at most the room down to the bottom of the window, so the
    // reset below stays visible. Measured with fractions: a limit rounded below the content shows a scrollbar
    // that has nothing to scroll.
    picker.addEventListener("toggle", () => {
      if (!picker.open) return;
      list.style.maxHeight = "none";
      list.style.overflowY = "hidden";
      // the height of the whole list: on phones the menu has a height of its own (filterbar.js), it squeezes the list
      const height = list.scrollHeight;
      const style = getComputedStyle(list);
      const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      const row = list.querySelector(".column-option")?.getBoundingClientRect().height || 2.4 * rem();
      const footerHeight = footer.getBoundingClientRect().height;
      let room = window.innerHeight - list.getBoundingClientRect().top - footerHeight - rem();
      const panelMax = parseFloat(getComputedStyle(panel).maxHeight);
      if (panelMax) room = Math.min(room, panelMax - footerHeight - 2);
      const limit = Math.max(3 * row + padding, Math.min(MAX_COLUMN_ROWS * row + padding, room));
      if (height <= limit + 0.5) {
        list.style.maxHeight = "none";
      } else {
        list.style.maxHeight = `${Math.floor(limit)}px`;
        list.style.overflowY = "auto";
      }
    });
    picker.append(summary, panel);
    // a new order or merge (chooser, dragged header, × of a merged one): stored, and the chooser follows it
    const changeLayout = (layout, focusKey = null, focusControl = null) => {
      settings.order = layout.order;
      settings.merged = layout.merged;
      settings.hidden = [...hiddenColumns(table, settings)];
      settings.known = columns.keys(table);
      columns.apply(table, layout, texts);
      applyHidden(table, settings);
      labelTables(table);
      applyWidths(table, settings);
      updateFits(table);
      saveTableSettings(table, settings);
      renderList();
      // the focus stays on the button or field that was used, now at the place of its column
      if (focusKey && focusControl) {
        const option = list.querySelector(`[data-column-option="${focusKey}"]`);
        const control = option?.querySelector(focusControl);
        (control && !control.disabled ? control : option?.querySelector("input"))?.focus();
      }
    };
    columns.enableDrag(table, (layout) => changeLayout(layout));
    panel.addEventListener("change", (event) => {
      const parent = event.target.closest("[data-column-parent]");
      if (parent) {
        const key = parent.closest("[data-column-option]").dataset.columnOption;
        changeLayout(columns.setParent(table, table._layout, key, parent.value), key, "[data-column-parent]");
        return;
      }
      const box = event.target.closest("input[data-column-key]");
      if (!box) return;
      settings.hidden = [...panel.querySelectorAll("input[data-column-key]")].filter((other) => !other.checked)
        .map((other) => other.dataset.columnKey);
      settings.known = columns.keys(table);
      applyHidden(table, settings);
      labelTables(table);
      applyWidths(table, settings);
      updateFits(table);
      saveTableSettings(table, settings);
    });
    panel.addEventListener("click", (event) => {
      const button = event.target.closest("[data-column-move]");
      if (!button) return;
      const key = button.closest("[data-column-option]").dataset.columnOption;
      const direction = Number(button.dataset.columnMove);
      changeLayout(columns.step(table, table._layout, key, direction), key, `[data-column-move="${direction}"]`);
    });
    resetAll.addEventListener("click", () => {
      Object.keys(settings).forEach((name) => delete settings[name]);
      saveTableSettings(table, settings);
      columns.apply(table, columns.defaultLayout(table), texts);
      applyHidden(table, settings);
      labelTables(table);
      renderList();
      applyWidths(table, settings);
      updateFits(table);
    });
    // a table of the drawer: its chooser above it, not in the filter bar of the list behind
    const inMain = !!table.closest("main");
    const tools = inMain ? document.querySelector("main [data-table-tools]") : null;
    const filters = inMain ? document.querySelector("main form.filters") : null;
    const slot = tools?.querySelector(".columns-picker[data-placeholder]");
    if (slot) {
      // the place kept for it while the rows loaded
      slot.replaceWith(picker);
    } else if (tools) {
      // the right end of the filter bar, after the views
      tools.append(picker);
    } else if (filters) {
      // at the right end; next to a button that is there already (e.g. "New local account"), not apart from it
      if (!filters.querySelector(":scope > .right")) picker.classList.add("right");
      filters.append(picker);
    } else {
      const tools = document.createElement("div");
      tools.className = "table-tools";
      tools.append(picker);
      (table.closest(".table-scroll") || table).before(tools);
    }
    const box = table.closest(".table-scroll");
    if (box) addScrollbar(box);
    updateFits(table);
    rememberLayout(table);
  }

  window.addEventListener("resize", () => document.querySelectorAll("table[data-table]").forEach(updateFits));
  // the drawer dragged wider or narrower (drawer.js): its tables
  document.addEventListener("drawerResized", () => document.querySelectorAll("#drawer table[data-table]").forEach(updateFits));

  // Keyboard in selectable tables: ↑/↓, Home/End move between rows, Space selects (Shift: a range), Enter opens
  function focusableRows(form) {
    return [...form.querySelectorAll("tbody tr")].filter((row) => row.querySelector('td.select input[type="checkbox"]'));
  }

  function setupRowFocus(root) {
    root.querySelectorAll("[data-select-form]").forEach((form) => {
      const rows = focusableRows(form);
      rows.forEach((row) => { if (!row.hasAttribute("tabindex")) row.tabIndex = -1; });
      if (rows.length && !rows.some((row) => row.tabIndex === 0)) rows[0].tabIndex = 0;
    });
  }

  document.addEventListener("focusin", (event) => {
    const row = event.target.closest?.("[data-select-form] tbody tr[tabindex]");
    if (!row) return;
    const form = row.closest("[data-select-form]");
    focusableRows(form).forEach((other) => { other.tabIndex = other === row ? 0 : -1; });
  });

  document.addEventListener("keydown", (event) => {
    const row = event.target.matches?.("[data-select-form] tbody tr[tabindex]") ? event.target : null;
    if (!row) return;
    const rows = focusableRows(row.closest("[data-select-form]"));
    const index = rows.indexOf(row);
    let next = null;
    if (event.key === "ArrowDown") next = rows[index + 1];
    else if (event.key === "ArrowUp") next = rows[index - 1];
    else if (event.key === "Home") next = rows[0];
    else if (event.key === "End") next = rows[rows.length - 1];
    else if (event.key === " ") {
      event.preventDefault();
      // like Ctrl-click (one row on or off) or Shift-click (a range)
      row.dispatchEvent(new MouseEvent("click", { bubbles: true, ctrlKey: !event.shiftKey, shiftKey: event.shiftKey }));
      return;
    } else if (event.key === "Enter") {
      event.preventDefault();
      // the entry, not the first link of the row (that may be another one now, e.g. the Mac of an event)
      columns.openRow(row);
      return;
    }
    if (next) {
      event.preventDefault();
      next.focus();
    }
  });

  // Rows are selected like files: click selects one row, Ctrl/⌘-click adds or removes one,
  // Shift-click selects the range since the last click. The checkboxes still toggle a single row.
  let lastChecked = null;

  function selectRange(boxes, box, checked) {
    const [from, to] = [boxes.indexOf(lastChecked), boxes.indexOf(box)].sort((a, b) => a - b);
    boxes.slice(from, to + 1).forEach((other) => {
      other.checked = checked;
    });
  }

  const INTERACTIVE = "a, button, input, select, textarea, label, summary, [hx-get], [hx-post]";

  // The device of the last press: Safari on iOS gives the click of a tap no pointerType, or "mouse", so the tap
  // would select only its row instead of adding it
  let lastPointer = "";
  document.addEventListener("pointerdown", (event) => { lastPointer = event.pointerType; }, true);
  document.addEventListener("touchstart", () => { lastPointer = "touch"; }, { capture: true, passive: true });

  function isTouch(event) {
    if (event.pointerType === "touch" || event.pointerType === "pen") return true;
    // a click from the keyboard (Space has its own handling)
    if (event.detail === 0) return false;
    if (lastPointer) return lastPointer === "touch" || lastPointer === "pen";
    return window.matchMedia("(hover: none)").matches;
  }

  // the first column shown as the link of the entry (columns.placeRowLinks): its own link opens it in the drawer;
  // with a modifier key or the middle button the browser opens the href (a new tab)
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a.row-open");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    event.stopPropagation();
    columns.openRow(link.closest("tr"));
  }, true);

  // a double-click opens the row (a click selects it, like files)
  document.addEventListener("dblclick", (event) => {
    const row = event.target.closest?.("table[data-table] tbody tr");
    if (!row || row.matches(".empty, .skeleton") || event.target.closest(INTERACTIVE)) return;
    window.getSelection()?.removeAllRanges();
    columns.openRow(row);
  });

  document.addEventListener("click", (event) => {
    // the second click of a double-click: the row stays selected, the double-click opens it
    if (event.detail > 1 && !event.target.closest?.(INTERACTIVE)) return;
    const clearButton = event.target.closest("[data-clear-selection]");
    if (clearButton) clearSelection(clearButton.closest("[data-select-form]"));
    const form = event.target.closest("[data-select-form]");
    const row = event.target.closest("tbody tr");
    if (!form || !row || !form.contains(row)) return;
    const box = row.querySelector('input[type="checkbox"]');
    if (!box || !rowBoxes(form).includes(box)) return;
    const boxes = rowBoxes(form);
    const canRange = event.shiftKey && lastChecked && boxes.includes(lastChecked) && lastChecked !== box;
    if (event.target === box) {
      if (canRange) selectRange(boxes, box, box.checked);
    } else {
      if (event.target.closest(INTERACTIVE)) return;
      if (!event.shiftKey && window.getSelection()?.toString()) return;
      if (canRange) {
        selectRange(boxes, box, true);
      } else if (event.ctrlKey || event.metaKey || isTouch(event)) {
        // touch has no ctrl key: a tap adds or removes the row, the others stay selected
        box.checked = !box.checked;
      } else {
        const onlyThis = box.checked && boxes.every((other) => other === box || !other.checked);
        boxes.forEach((other) => {
          other.checked = false;
        });
        box.checked = !onlyThis;
      }
    }
    lastChecked = box;
    selectionChanged(form);
  });

  // no text selection while shift- or ctrl-clicking the rows
  document.addEventListener("mousedown", (event) => {
    // a double-click doesn't select a word either
    if ((event.shiftKey || event.ctrlKey || event.metaKey || event.detail > 1)
        && event.target.closest("table[data-table] tbody, [data-select-form] tbody")
        && !event.target.closest(INTERACTIVE)) {
      event.preventDefault();
    }
  });

  document.addEventListener("change", (event) => {
    const selectForm = event.target.closest("[data-select-form]");
    if (!selectForm) return;
    if (event.target.matches("[data-select-all]")) {
      rowBoxes(selectForm).forEach((box) => {
        box.checked = event.target.checked;
      });
    }
    selectionChanged(selectForm);
  });

  // Escape clears the selection, unless it is typed in a field (drawer.js closes an open drawer first)
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.target.closest('input:not([type="checkbox"]), select, textarea')) return;
    document.querySelectorAll("[data-select-form]").forEach((form) => {
      if (form.querySelector("[data-bulkbar].visible")) clearSelection(form);
    });
  });

  // The rows of the list load after the page (console/_rows.html): the column chooser gets its place in the filter
  // bar already, so the bar doesn't move when they arrive
  function keepPickerPlace(root) {
    const tools = document.querySelector("main [data-table-tools]");
    if (!root.querySelector?.(".rows-loading") || !tools || tools.querySelector(".columns-picker")) return;
    const texts = document.getElementById("table-texts")?.dataset || {};
    const slot = document.createElement("details");
    slot.className = "picker down columns-picker";
    slot.dataset.placeholder = "";
    const summary = document.createElement("summary");
    summary.className = "button";
    summary.textContent = texts.columns || "Columns";
    summary.setAttribute("aria-disabled", "true");
    summary.tabIndex = -1;
    summary.addEventListener("click", (event) => event.preventDefault());
    slot.append(summary);
    tools.append(slot);
  }

  SantaConsole.onInit((root) => {
    keepPickerPlace(root);
    labelTables(root);
    addSelectAllCards(root);
    root.querySelectorAll("[data-bulkbar]").forEach((bar) => barObserver?.observe(bar));
    root.querySelectorAll("table[data-table]").forEach(setupTable);
    setupRowFocus(root);
    root.querySelectorAll("[data-select-form]").forEach(updateSelection);
  });
  SantaConsole.onSwap((target) => {
    // a row swapped as a whole (outerHTML, e.g. its switch): the old one is gone, the new one needs its labels
    labelTables(target.isConnected ? target.closest("table") || target : document);
    addSelectAllCards(document);
    setupRowFocus(document);
    updateAllSelections();
  });

  // the widths as they are when the page is left (also after the user changed them)
  const rememberAll = () => document.querySelectorAll("main table[data-table][data-table-ready]").forEach((table) => {
    if (!table.closest(".rows-loading")) rememberLayout(table);
  });
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target === document.body) rememberAll();
  });
  window.addEventListener("pagehide", rememberAll);

  SantaConsole.table = { rowBoxes, updateSelection, updateAllSelections, labelTables, setupRowFocus, currentLayout };
})();
