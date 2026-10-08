// The tables of the lists: selection and bulk bar, column widths and chooser, labels for phones, keyboard
(function () {
  "use strict";

  const { rem } = SantaConsole;

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

  function applyHidden(table, settings) {
    const optional = (table.dataset.optional || "").split(" ").filter(Boolean);
    const hidden = new Set(settings.hidden || optional);
    let style = document.getElementById(`table-style-${table.dataset.table}`);
    if (!style) {
      style = document.createElement("style");
      style.id = `table-style-${table.dataset.table}`;
      document.head.append(style);
    }
    const selector = `table[data-table="${table.dataset.table}"]`;
    // nth-child: also for rows that come later (live updates, toggles)
    style.textContent = tableColumns(table).filter((column) => hidden.has(column.key))
      .map((column) => `${selector} tr:not(.empty) > :nth-child(${column.index + 1}) { display: none; }`).join("\n");
    table.dataset.shown = tableColumns(table).filter((column) => !hidden.has(column.key))
      .map((column) => column.key).join(" ");
    return hidden;
  }

  function updateFits(table) {
    const box = table.closest(".table-scroll");
    if (!box) return;
    box.classList.remove("fits");
    box.classList.toggle("fits", box.scrollWidth <= box.clientWidth + 1);
  }

  // the width below which the tables are cards (table.css)
  const cardLayout = window.matchMedia("(max-width: 45em)");

  // the column chooser shows at most this many columns without scrolling (the rules have 9)
  const MAX_COLUMN_ROWS = 9;

  function setupTable(table) {
    if (table.dataset.tableReady || !table.tHead) return;
    table.dataset.tableReady = "1";
    const texts = document.getElementById("table-texts")?.dataset || {};
    const settings = tableSettings(table);
    [...table.tHead.rows[0].cells].forEach((th) => {
      const name = th.textContent.trim();
      // a column without a name (e.g. the buttons) can't be chosen or resized: it would be an empty entry
      if (!th.dataset.col && !th.classList.contains("select") && name) {
        th.dataset.col = name.toLowerCase().replace(/[^a-z0-9]+/g, "-") || `col${th.cellIndex}`;
      }
    });
    tableColumns(table).forEach(({ th, key }) => {
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
    applyHidden(table, settings);
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
    const hidden = applyHidden(table, settings);
    tableColumns(table).forEach(({ th, key }) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !hidden.has(key);
      box.dataset.columnKey = key;
      label.append(box, ` ${th.textContent.trim()}`);
      list.append(label);
    });
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
      const height = list.getBoundingClientRect().height;
      const style = getComputedStyle(list);
      const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      const row = list.querySelector("label")?.getBoundingClientRect().height || 2.4 * rem();
      const room = window.innerHeight - list.getBoundingClientRect().top - footer.getBoundingClientRect().height - rem();
      const limit = Math.max(3 * row + padding, Math.min(MAX_COLUMN_ROWS * row + padding, room));
      if (height <= limit + 0.5) {
        list.style.maxHeight = "none";
      } else {
        list.style.maxHeight = `${Math.floor(limit)}px`;
        list.style.overflowY = "auto";
      }
    });
    picker.append(summary, panel);
    panel.addEventListener("change", (event) => {
      const box = event.target.closest("[data-column-key]");
      if (!box) return;
      settings.hidden = [...panel.querySelectorAll("[data-column-key]")].filter((other) => !other.checked)
        .map((other) => other.dataset.columnKey);
      applyHidden(table, settings);
      applyWidths(table, settings);
      updateFits(table);
      saveTableSettings(table, settings);
    });
    resetAll.addEventListener("click", () => {
      Object.keys(settings).forEach((name) => delete settings[name]);
      saveTableSettings(table, settings);
      const defaults = applyHidden(table, settings);
      panel.querySelectorAll("[data-column-key]").forEach((box) => { box.checked = !defaults.has(box.dataset.columnKey); });
      applyWidths(table, settings);
      updateFits(table);
    });
    const tools = document.querySelector("main [data-table-tools]");
    const filters = document.querySelector("main form.filters");
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
    updateFits(table);
  }

  window.addEventListener("resize", () => document.querySelectorAll("table[data-table]").forEach(updateFits));

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
      row.querySelector("a[hx-get]")?.click();
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

  document.addEventListener("click", (event) => {
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
    if ((event.shiftKey || event.ctrlKey || event.metaKey) && event.target.closest("[data-select-form] tbody")
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

  SantaConsole.table = { rowBoxes, updateSelection, updateAllSelections, labelTables, setupRowFocus };
})();
