// The column layout of the lists (table.js keeps it per browser and in the saved views): the order of the columns,
// and columns merged into another one (their values under its own, as the comment under the identifier).
// A layout is { order: [keys], merged: {child: parent} }; the keys are the data-col of the header cells.
(function () {
  "use strict";

  const { rem } = SantaConsole;

  // The columns as the server rendered them, read once: rows arrive in this order too (pages, live updates, a row
  // swapped after its switch), so their cells get their key by position.
  function markup(table) {
    if (table._columns) return table._columns;
    table._columns = [...table.tHead.rows[0].cells].map((th, index) => {
      const name = th.textContent.trim();
      // a column without a name (the checkboxes, the buttons) can't be chosen, moved or resized
      if (!th.dataset.col && !th.classList.contains("select") && name) {
        th.dataset.col = name.toLowerCase().replace(/[^a-z0-9]+/g, "-") || `col${index}`;
      }
      return { key: th.dataset.col || null, th, label: name, index };
    });
    return table._columns;
  }

  function keys(table) {
    return markup(table).filter((column) => column.key).map((column) => column.key);
  }

  function labelOf(table, key) {
    return markup(table).find((column) => column.key === key)?.label || key;
  }

  // the merges the list has without a choice of the user (data-merged="child:parent …")
  function defaultLayout(table) {
    const merged = {};
    (table.dataset.merged || "").split(" ").filter(Boolean).forEach((pair) => {
      const [child, parent] = pair.split(":");
      merged[child] = parent;
    });
    return normalize(table, { order: keys(table), merged });
  }

  // A stored layout for the columns there are now: unknown keys go, new columns keep their place next to the column
  // before them, merges point to a column of its own (no chains), and the merged ones follow their column.
  function normalize(table, layout) {
    const all = keys(table);
    const known = new Set(all);
    const order = [...new Set((layout?.order || []).filter((key) => known.has(key)))];
    all.forEach((key, index) => {
      if (order.includes(key)) return;
      const before = all.slice(0, index).reverse().find((other) => order.includes(other));
      order.splice(before ? order.indexOf(before) + 1 : 0, 0, key);
    });
    const merged = {};
    Object.entries(layout?.merged || {}).forEach(([child, parent]) => {
      if (known.has(child) && known.has(parent) && child !== parent) merged[child] = parent;
    });
    Object.keys(merged).forEach((child) => {
      const seen = new Set([child]);
      let parent = merged[child];
      while (merged[parent] && !seen.has(parent)) {
        seen.add(parent);
        parent = merged[parent];
      }
      if (seen.has(parent)) delete merged[child];
      else merged[child] = parent;
    });
    const tops = order.filter((key) => !merged[key]);
    return { order: tops.flatMap((top) => [top, ...order.filter((key) => merged[key] === top)]), merged };
  }

  function tops(layout) {
    return layout.order.filter((key) => !layout.merged[key]);
  }

  function children(layout, top) {
    return layout.order.filter((key) => layout.merged[key] === top);
  }

  // the column with the ones merged into it
  function group(layout, top) {
    return [top, ...children(layout, top)];
  }

  // key (with its merged ones) before or after the column target
  function move(table, layout, key, target, after) {
    if (key === target || layout.merged[key] || layout.merged[target]) return layout;
    const moving = group(layout, key);
    const rest = tops(layout).filter((top) => top !== key);
    const index = rest.indexOf(target) + (after ? 1 : 0);
    rest.splice(index, 0, key);
    return normalize(table, { order: rest.flatMap((top) => (top === key ? moving : group(layout, top))),
                              merged: layout.merged });
  }

  // key under the column parent, at its end; the columns merged into key go along
  function merge(table, layout, key, parent) {
    if (key === parent || layout.merged[parent]) return layout;
    const merged = { ...layout.merged };
    const moving = layout.merged[key] ? [key] : group(layout, key);
    moving.forEach((other) => { merged[other] = parent; });
    const order = layout.order.filter((other) => !moving.includes(other));
    const end = order.indexOf(parent) + 1 + children({ order, merged }, parent).length;
    order.splice(end, 0, ...moving);
    return normalize(table, { order, merged });
  }

  // key in a column of its own again, right after the one it was under
  function split(table, layout, key) {
    const parent = layout.merged[key];
    if (!parent) return layout;
    const merged = { ...layout.merged };
    delete merged[key];
    const order = layout.order.filter((other) => other !== key);
    order.splice(order.indexOf(parent) + children({ order, merged }, parent).length + 1, 0, key);
    return normalize(table, { order, merged });
  }

  // one step to the left / right (a column) or up / down (merged into another one)
  function step(table, layout, key, direction) {
    const parent = layout.merged[key];
    const siblings = parent ? children(layout, parent) : tops(layout);
    const index = siblings.indexOf(key);
    const other = siblings[index + direction];
    if (!other) return layout;
    if (!parent) return move(table, layout, key, other, direction > 0);
    const order = [...layout.order];
    const [a, b] = [order.indexOf(key), order.indexOf(other)];
    [order[a], order[b]] = [order[b], order[a]];
    return normalize(table, { order, merged: layout.merged });
  }

  function setParent(table, layout, key, parent) {
    if (!parent) return split(table, layout, key);
    if (layout.merged[key] === parent) return layout;
    return merge(table, layout.merged[key] ? split(table, layout, key) : layout, key, parent);
  }

  // The cells of a row by key: the ones without a key (checkbox, buttons) keep their side
  function rowCells(table, row) {
    if (row._cells) return row._cells;
    const columns = markup(table);
    const cells = [...row.children];
    // a row of its own kind (e.g. "nothing found" over all columns) stays as it is
    if (cells.length !== columns.length) return null;
    const firstKey = columns.findIndex((column) => column.key);
    const byKey = {};
    const lead = [];
    const trail = [];
    cells.forEach((cell, index) => {
      const key = cell.dataset.col || columns[index].key;
      if (key) {
        cell.dataset.col = key;
        byKey[key] = cell;
      } else {
        (index < firstKey ? lead : trail).push(cell);
      }
    });
    row._cells = { byKey, lead, trail };
    // the link that opens the entry (identifier, file, Mac, name …): the first one to the drawer in the first column
    // of the markup, else "Open" of its ⋯ menu. Found before the cells move, it stays known when merged or hidden.
    row._open = entryLink(row, byKey[columns[firstKey]?.key]);
    return row._cells;
  }

  function entryLink(row, firstCell) {
    return firstCell?.querySelector('a[hx-target="#drawer"]') || row.querySelector('.row-menu a[hx-target="#drawer"]');
  }

  // A double-click, Enter or the link of the first column open the entry: its own link does it (htmx), also when it
  // is merged into another column or hidden
  function openRow(row) {
    const link = row._open || entryLink(row, row.querySelector("td:not(.select)"));
    link?.click();
  }

  const INTERACTIVE = "a, button, input, select, textarea, summary, [hx-get], [hx-post]";

  // The entry link is not at the start of the row (moved, merged into another column, hidden): the text of the first
  // column shown becomes a link to the entry too. Not a cell with links or buttons of its own (no link in a link):
  // a double-click, Enter and the ⋯ menu open it there.
  function placeRowLink(row, hidden) {
    row.querySelectorAll("a.row-open").forEach((link) => link.replaceWith(...link.childNodes));
    if (!row._open) return;
    const first = [...row.children].find((cell) => cell.dataset.col && !hidden.has(cell.dataset.col));
    if (!first || (row._open.closest("td") === first && !row._open.closest(".merged-part"))) return;
    const own = [...first.childNodes].filter((node) => !node.classList?.contains("merged-part"));
    if (!own.some((node) => node.textContent.trim())) return;
    if (own.some((node) => node.matches?.(INTERACTIVE) || node.querySelector?.(INTERACTIVE))) return;
    const link = document.createElement("a");
    link.className = "row-open";
    link.href = row._open.getAttribute("href");
    own[0].before(link);
    link.append(...own);
  }

  function placeRowLinks(table, hidden = new Set()) {
    if (!table._layout) return;
    [...(table.tBodies[0]?.rows || [])].forEach((row) => {
      if (row._cells) placeRowLink(row, hidden);
    });
  }

  function layoutRow(table, row, layout, signature) {
    if (row._layout === signature || row.matches(".empty, .skeleton")) return;
    const cells = rowCells(table, row);
    if (!cells) return;
    // back to one cell per column first, then merged as the layout says
    row.querySelectorAll(":scope > td > .merged-part[data-col]").forEach((part) => {
      cells.byKey[part.dataset.col]?.append(...part.childNodes);
      part.remove();
    });
    const shown = tops(layout).map((top) => {
      const cell = cells.byKey[top];
      children(layout, top).forEach((key) => {
        const child = cells.byKey[key];
        const part = document.createElement("div");
        part.className = `merged-part ${child.className}`.trim();
        part.dataset.col = key;
        part.append(...child.childNodes);
        // nothing to show (no e-mail, no description): no empty line under the value
        part.classList.toggle("empty", !part.textContent.trim() && !part.querySelector("img, svg, input"));
        cell.append(part);
      });
      return cell;
    });
    row.replaceChildren(...cells.lead, ...shown, ...cells.trail);
    row._layout = signature;
  }

  function layoutHeader(table, layout, texts) {
    const columns = markup(table);
    const firstKey = columns.findIndex((column) => column.key);
    const byKey = Object.fromEntries(columns.filter((column) => column.key).map((column) => [column.key, column.th]));
    const lead = columns.filter((column) => !column.key && column.index < firstKey).map((column) => column.th);
    const trail = columns.filter((column) => !column.key && column.index > firstKey).map((column) => column.th);
    const shown = tops(layout).map((top) => {
      const th = byKey[top];
      th.querySelectorAll(":scope > .merged-label").forEach((label) => label.remove());
      const handle = th.querySelector(":scope > .col-resize");
      children(layout, top).forEach((key) => {
        const label = document.createElement("span");
        label.className = "merged-label";
        label.dataset.col = key;
        // a sortable column stays sortable under the other one
        const sort = byKey[key].querySelector(":scope > a.sort");
        label.append(" / ", sort ? sort.cloneNode(true) : labelOf(table, key));
        const button = document.createElement("button");
        button.type = "button";
        button.className = "merge-split";
        button.dataset.split = key;
        button.textContent = "×";
        button.title = texts.split || "Own column";
        button.setAttribute("aria-label", `${texts.split || "Own column"}: ${labelOf(table, key)}`);
        label.append(button);
        th.insertBefore(label, handle);
      });
      return th;
    });
    table.tHead.rows[0].replaceChildren(...lead, ...shown, ...trail);
  }

  // The layout on the header and on every row (also the ones that come later: layoutRows)
  function apply(table, stored, texts = {}) {
    const layout = normalize(table, stored);
    table._layout = layout;
    table._layoutSignature = JSON.stringify(layout);
    layoutHeader(table, layout, texts);
    layoutRows(table);
    return layout;
  }

  function layoutRows(table) {
    if (!table._layout) return;
    [...(table.tBodies[0]?.rows || [])].forEach((row) => layoutRow(table, row, table._layout, table._layoutSignature));
  }

  // The label of a column with the ones merged into it, shown: "Identifier / Comment" (cards on phones, chooser)
  function groupLabel(table, top, hidden = new Set()) {
    const layout = table._layout;
    const parts = [top, ...(layout ? children(layout, top) : [])].filter((key, index) => !index || !hidden.has(key));
    return parts.map((key) => labelOf(table, key)).join(" / ");
  }

  // Dragging a header: on the left or right third of another one it goes before or after it, on the middle it is
  // merged under it. Not on phones (cards, no header); the chooser does the same with buttons.
  function enableDrag(table, onChange) {
    let drag = null;

    function topHeaders() {
      return [...table.tHead.rows[0].cells].filter((th) => th.dataset.col && th.getClientRects().length);
    }

    function targetAt(x) {
      const th = topHeaders().find((cell) => {
        const rect = cell.getBoundingClientRect();
        return x >= rect.left && x < rect.right;
      });
      if (!th || th === drag.th) return null;
      const rect = th.getBoundingClientRect();
      const share = (x - rect.left) / rect.width;
      const kind = share < 0.3 ? "before" : share > 0.7 ? "after" : "merge";
      return { th, key: th.dataset.col, kind, rect };
    }

    function showMarker(target) {
      const marker = drag.marker;
      if (!target) {
        marker.hidden = true;
        return;
      }
      const tableRect = table.getBoundingClientRect();
      const top = Math.max(target.rect.top, 0);
      const bottom = Math.min(tableRect.bottom, window.innerHeight);
      marker.hidden = false;
      marker.classList.toggle("merge", target.kind === "merge");
      const width = target.kind === "merge" ? target.rect.width : 0.2 * rem();
      const left = target.kind === "merge" ? target.rect.left
        : (target.kind === "before" ? target.rect.left : target.rect.right) - width / 2;
      Object.assign(marker.style, { left: `${left / rem()}rem`, top: `${top / rem()}rem`, width: `${width / rem()}rem`,
                                    height: `${Math.max(bottom - top, 0) / rem()}rem` });
    }

    function end(commit) {
      if (!drag) return;
      const { th, marker, ghost, target, started } = drag;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
      window.removeEventListener("keydown", onKey, true);
      marker.remove();
      ghost.remove();
      th.classList.remove("col-dragging");
      document.body.classList.remove("col-drag-active");
      drag = null;
      if (!started) return;
      // the click that ends the drag doesn't sort
      const swallow = (event) => { event.preventDefault(); event.stopPropagation(); };
      window.addEventListener("click", swallow, { capture: true, once: true });
      setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
      if (!commit || !target) return;
      const layout = table._layout;
      const key = th.dataset.col;
      onChange(target.kind === "merge" ? merge(table, layout, key, target.key)
        : move(table, layout, key, target.key, target.kind === "after"));
    }

    function onMove(event) {
      if (!drag.started) {
        if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 0.5 * rem()) return;
        drag.started = true;
        drag.th.classList.add("col-dragging");
        document.body.classList.add("col-drag-active");
        document.body.append(drag.marker, drag.ghost);
      }
      event.preventDefault();
      drag.target = targetAt(event.clientX);
      showMarker(drag.target);
      drag.ghost.style.left = `${event.clientX / rem() + 0.75}rem`;
      drag.ghost.style.top = `${event.clientY / rem() + 0.75}rem`;
    }

    const onUp = () => end(true);
    const onCancel = () => end(false);

    function onKey(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      end(false);
    }

    table.tHead.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.pointerType === "touch" || drag) return;
      const th = event.target.closest("th[data-col]");
      if (!th || event.target.closest(".col-resize, .merge-split, input, button")) return;
      if (window.matchMedia("(max-width: 45em)").matches) return;
      const marker = document.createElement("div");
      marker.className = "col-drop-marker";
      marker.hidden = true;
      const ghost = document.createElement("div");
      ghost.className = "col-drag-ghost";
      ghost.textContent = groupLabel(table, th.dataset.col);
      drag = { th, x: event.clientX, y: event.clientY, started: false, target: null, marker, ghost };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onCancel);
      window.addEventListener("keydown", onKey, true);
    });
    // a link can't be dragged as a link while the column is
    table.tHead.addEventListener("dragstart", (event) => event.preventDefault());

    table.tHead.addEventListener("click", (event) => {
      const button = event.target.closest(".merge-split");
      if (!button || !table._layout) return;
      event.preventDefault();
      onChange(split(table, table._layout, button.dataset.split));
    });
  }

  SantaConsole.columns = {
    markup, keys, labelOf, defaultLayout, normalize, tops, children, move, merge, split, step, setParent, apply,
    layoutRows, groupLabel, enableDrag, openRow, placeRowLinks,
  };
})();
