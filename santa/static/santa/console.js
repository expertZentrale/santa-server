// Small helpers for the console pages. No build step: plain JS next to htmx.
(function () {
  "use strict";

  function controlValue(form, name) {
    const controls = form.querySelectorAll(`[name="${name}"], [name$="-${name}"]`);
    for (const control of controls) {
      if (control.type === "radio") {
        if (control.checked) return control.value;
      } else if (control.type === "checkbox") {
        return control.checked ? "on" : "";
      } else {
        return control.value;
      }
    }
    return "";
  }

  // data-show-when="policy=BLOCKLIST|CEL", data-hide-when="is_global"
  function updateConditional(root) {
    root.querySelectorAll("[data-show-when], [data-hide-when]").forEach((element) => {
      const form = element.closest("form") || document;
      let visible = true;
      if (element.dataset.showWhen) {
        const [name, values] = element.dataset.showWhen.split("=");
        visible = values.split("|").includes(controlValue(form, name));
      }
      if (element.dataset.hideWhen) {
        visible = visible && !controlValue(form, element.dataset.hideWhen);
      }
      element.hidden = !visible;
    });
  }

  // Selection and bulk bar of the lists: the bar floats at the bottom, the table never moves
  function rowBoxes(form) {
    return [...form.querySelectorAll('tbody input[type="checkbox"]')];
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
    const all = form.querySelector("[data-select-all]");
    if (all) all.checked = checked > 0 && checked === boxes.length;
    document.body.classList.toggle(
      "has-bulkbar", !!document.querySelector("[data-bulkbar].visible"));
  }

  function clearSelection(form) {
    rowBoxes(form).forEach((box) => {
      box.checked = false;
    });
    updateSelection(form);
  }

  // The drawer with the event details
  function closeDrawer() {
    const drawer = document.getElementById("drawer");
    drawer.classList.remove("open");
    drawer.replaceChildren();
  }

  function copy(text, element) {
    const done = () => {
      element.classList.add("copied");
      setTimeout(() => element.classList.remove("copied"), 1200);
    };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text.trim()).then(done);
      return;
    }
    // the Clipboard API only exists on HTTPS and localhost
    const area = document.createElement("textarea");
    area.value = text.trim();
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    if (document.execCommand("copy")) done();
    area.remove();
  }

  // Catalog suggestions
  function debounce(fn, delay) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function fetchSuggestions(url, kind, query, box) {
    if (query.trim().length < 2) {
      box.replaceChildren();
      box.classList.remove("open");
      return;
    }
    const params = new URLSearchParams({ kind, q: query });
    fetch(`${url}?${params}`, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
      .then((response) => (response.ok ? response.text() : ""))
      .then((html) => {
        // server-rendered, escaped template
        box.innerHTML = html;
        box.classList.toggle("open", html.trim() !== "");
      })
      .catch(() => box.classList.remove("open"));
  }

  function makeIcon(iconUrl) {
    const img = document.createElement("img");
    img.src = iconUrl;
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    return img;
  }

  function setupIdentifiers(form) {
    const widget = form.querySelector("[data-identifiers-widget]");
    if (!widget) return;
    const chips = widget.querySelector("[data-chips]");
    const textarea = widget.querySelector("textarea");
    const search = widget.querySelector("[data-search]");
    const box = widget.querySelector("[data-suggestions]");
    const picked = form.querySelector('input[name="picked"]');
    const kindSelect = form.querySelector('select[name="kind"]');
    const searchable = (form.dataset.searchable || "").split(",");
    const pickedData = {};

    function sync() {
      textarea.value = [...chips.children].map((chip) => chip.dataset.identifier).join("\n");
      if (picked) picked.value = JSON.stringify(pickedData);
    }

    function addChip(identifier, name, iconUrl) {
      identifier = identifier.trim();
      if (!identifier || [...chips.children].some((chip) => chip.dataset.identifier === identifier)) return;
      const chip = document.createElement("li");
      chip.className = "chip";
      chip.dataset.identifier = identifier;
      if (iconUrl) chip.append(makeIcon(iconUrl));
      const text = document.createElement("span");
      text.className = "chip-text";
      const strong = document.createElement("strong");
      strong.textContent = name || identifier;
      text.append(strong);
      if (name && name !== identifier) {
        const code = document.createElement("code");
        code.textContent = identifier;
        text.append(code);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.setAttribute("aria-label", `${search.dataset.removeLabel || "Remove"} ${identifier}`);
      remove.textContent = "×";
      chip.append(text, remove);
      chips.append(chip);
      if (name || iconUrl) pickedData[identifier] = { name, icon_url: iconUrl };
      sync();
    }

    const lookup = debounce(() => {
      const kind = kindSelect ? kindSelect.value : "";
      if (!searchable.includes(kind)) {
        box.replaceChildren();
        box.classList.remove("open");
        return;
      }
      fetchSuggestions(form.dataset.catalogUrl, kind, search.value, box);
    }, 300);

    search.addEventListener("input", lookup);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const first = box.querySelector(".suggestion[aria-selected=true]");
        if (first) first.click();
        else if (search.value.trim()) {
          search.value.split(/[\n,]/).forEach((value) => addChip(value, "", ""));
          search.value = "";
          box.classList.remove("open");
        }
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Escape") {
        box.classList.remove("open");
      }
    });
    box.addEventListener("click", (event) => {
      const option = event.target.closest(".suggestion");
      if (!option) return;
      addChip(option.dataset.identifier, option.dataset.name, option.dataset.icon);
      search.value = "";
      box.classList.remove("open");
      search.focus();
    });
    chips.addEventListener("click", (event) => {
      const remove = event.target.closest(".chip-remove");
      if (!remove) return;
      const chip = remove.closest(".chip");
      delete pickedData[chip.dataset.identifier];
      chip.remove();
      sync();
    });
    kindSelect?.addEventListener("change", () => {
      search.placeholder = searchable.includes(kindSelect.value)
        ? search.dataset.placeholderSearch
        : search.dataset.placeholderPlain;
      lookup();
    });
    // an identifier typed but not confirmed with Enter is kept too
    form.addEventListener("submit", () => {
      if (search.value.trim()) addChip(search.value, "", "");
    });
  }

  // Request form: several packages, from any catalog, as chips
  function setupPackageRequest(form) {
    const chips = form.querySelector("[data-chips]");
    const search = form.querySelector("[data-search]");
    const box = form.querySelector("[data-suggestions]");
    const kind = form.querySelector("[data-kind]");
    const hidden = form.querySelector('input[name="packages"]');
    if (!chips || !search || !hidden) return;
    const searchable = (form.dataset.searchable || "").split(",");
    let packages = [];
    try {
      packages = JSON.parse(hidden.value || "[]");
    } catch (error) {
      packages = [];
    }

    function render() {
      chips.replaceChildren(...packages.map((item, index) => {
        const chip = document.createElement("li");
        chip.className = "chip";
        if (item.icon_url) chip.append(makeIcon(item.icon_url));
        const text = document.createElement("span");
        text.className = "chip-text";
        const strong = document.createElement("strong");
        strong.textContent = item.name || item.identifier;
        const code = document.createElement("code");
        code.textContent = `${kind.querySelector(`option[value="${item.kind}"]`)?.textContent || item.kind} · ${item.identifier}`;
        text.append(strong, code);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "chip-remove";
        remove.dataset.index = index;
        remove.setAttribute("aria-label", `${search.dataset.removeLabel || "Remove"} ${item.identifier}`);
        remove.textContent = "×";
        chip.append(text, remove);
        return chip;
      }));
      hidden.value = JSON.stringify(packages);
    }

    function add(identifier, name, iconUrl) {
      identifier = identifier.trim();
      if (!identifier || packages.some((p) => p.kind === kind.value && p.identifier === identifier)) return;
      packages.push({ kind: kind.value, identifier, name: name || "", icon_url: iconUrl || "" });
      render();
    }

    const lookup = debounce(() => {
      if (searchable.includes(kind.value)) fetchSuggestions(form.dataset.catalogUrl, kind.value, search.value, box);
      else box.classList.remove("open");
    }, 300);
    search.addEventListener("input", lookup);
    kind.addEventListener("change", lookup);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const selected = box.querySelector(".suggestion[aria-selected=true]");
        if (selected) selected.click();
        else if (search.value.trim()) {
          add(search.value, "", "");
          search.value = "";
          box.classList.remove("open");
        }
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Escape") {
        box.classList.remove("open");
      }
    });
    box.addEventListener("click", (event) => {
      const option = event.target.closest(".suggestion");
      if (!option) return;
      add(option.dataset.identifier, option.dataset.name, option.dataset.icon);
      search.value = "";
      box.classList.remove("open");
      search.focus();
    });
    chips.addEventListener("click", (event) => {
      const remove = event.target.closest(".chip-remove");
      if (!remove) return;
      packages.splice(Number(remove.dataset.index), 1);
      render();
    });
    form.addEventListener("submit", () => {
      if (search.value.trim()) add(search.value, "", "");
    });
    render();
  }

  function moveSelection(box, step) {
    const options = [...box.querySelectorAll(".suggestion")];
    if (!options.length) return;
    let index = options.findIndex((option) => option.getAttribute("aria-selected") === "true");
    options.forEach((option) => option.setAttribute("aria-selected", "false"));
    index = Math.min(Math.max(index + step, 0), options.length - 1);
    options[index].setAttribute("aria-selected", "true");
    options[index].scrollIntoView({ block: "nearest" });
  }

  // Tags typed as "a, b": suggest the existing tags for the part after the last comma
  function tagNames() {
    const element = document.getElementById("tag-names");
    try {
      return element ? JSON.parse(element.textContent) : [];
    } catch (error) {
      return [];
    }
  }

  function setupTagInput(input, names) {
    if (input.dataset.tagInput) return;
    input.dataset.tagInput = "1";
    input.setAttribute("autocomplete", "off");
    const wrapper = document.createElement("div");
    wrapper.className = "combobox";
    input.replaceWith(wrapper);
    const box = document.createElement("div");
    box.className = "suggestions";
    box.setAttribute("role", "listbox");
    wrapper.append(input, box);

    function parts() {
      return input.value.split(",").map((part) => part.trim());
    }

    function show() {
      const current = parts();
      const typed = current[current.length - 1].toLowerCase();
      const chosen = new Set(current.slice(0, -1).map((part) => part.toLowerCase()));
      const matches = names.filter((name) => !chosen.has(name.toLowerCase())
                                             && (!typed || name.toLowerCase().includes(typed))).slice(0, 8);
      box.replaceChildren(...matches.map((name) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "suggestion tag-suggestion";
        option.setAttribute("role", "option");
        option.dataset.tag = name;
        option.textContent = name;
        return option;
      }));
      box.classList.toggle("open", matches.length > 0 && document.activeElement === input);
    }

    function choose(name) {
      const current = parts().slice(0, -1).filter(Boolean);
      current.push(name);
      input.value = current.join(", ") + ", ";
      box.classList.remove("open");
      input.focus();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    input.addEventListener("input", show);
    input.addEventListener("focus", show);
    input.addEventListener("keydown", (event) => {
      if (!box.classList.contains("open")) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter") {
        const selected = box.querySelector(".suggestion[aria-selected=true]");
        if (selected) {
          event.preventDefault();
          choose(selected.dataset.tag);
        }
      } else if (event.key === "Escape") {
        box.classList.remove("open");
      }
    });
    // mousedown: before the input loses the focus
    box.addEventListener("mousedown", (event) => {
      const option = event.target.closest(".suggestion");
      if (option) {
        event.preventDefault();
        choose(option.dataset.tag);
      }
    });
    input.addEventListener("blur", () => setTimeout(() => box.classList.remove("open"), 150));
  }

  function setupTagInputs(root) {
    const names = tagNames();
    if (!names.length) return;
    root.querySelectorAll('input[name$="new_tags"], input[data-apply="new_tags"], input[data-tags]')
        .forEach((input) => setupTagInput(input, names));
  }

  // Approve a package request: the fields of the new package rule only when a package goes there
  function updateNewRuleFields(root) {
    root.querySelectorAll("[data-new-rule-fields]").forEach((fields) => {
      const form = fields.closest("form");
      const needed = [...form.querySelectorAll('select[name^="target_"]')].some((select) => {
        const approve = form.querySelector(`input[name="approve_${select.name.slice(7)}"]`);
        return select.value === "new" && (!approve || approve.checked);
      });
      fields.hidden = !needed;
    });
  }

  // Column names on every cell: on phones the rows become cards with "label: value"
  function labelTables(root) {
    root.querySelectorAll("table.table").forEach((table) => {
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

  // Rule type preview: which identifier of the binary the rule will use
  function updateRulePreviews(root) {
    root.querySelectorAll("select[data-identifiers]").forEach((select) => {
      let identifiers = {};
      try {
        identifiers = JSON.parse(select.dataset.identifiers);
      } catch (error) {
        return;
      }
      const container = select.closest("[data-field], td") || select.parentElement;
      let preview = container.querySelector("[data-rule-preview]");
      if (!preview) {
        preview = document.createElement("small");
        preview.className = "rule-preview";
        preview.dataset.rulePreview = "";
        select.after(preview);
      }
      const value = identifiers[select.value];
      preview.replaceChildren();
      if (value) {
        const code = document.createElement("code");
        code.textContent = value;
        preview.append(select.dataset.previewLabel || "", " ", code);
      }
    });
  }

  function init(root) {
    labelTables(root);
    updateRulePreviews(root);
    updateConditional(root);
    updateNewRuleFields(root);
    setupTagInputs(root);
    root.querySelectorAll("[data-source-form]").forEach(setupIdentifiers);
    root.querySelectorAll("[data-package-form]").forEach(setupPackageRequest);
    root.querySelectorAll("[data-select-form]").forEach(updateSelection);
  }

  // The server shows the times in the time zone of the browser (unless the profile sets one)
  function sendTimeZone() {
    let zone = "";
    try {
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      return;
    }
    const current = document.cookie.split("; ").find((item) => item.startsWith("santa_tz="));
    if (!zone || current === `santa_tz=${encodeURIComponent(zone)}`) return;
    const secure = location.protocol === "https:" ? "; secure" : "";
    document.cookie = `santa_tz=${encodeURIComponent(zone)}; path=/; max-age=31536000; samesite=lax${secure}`;
  }

  sendTimeZone();
  document.addEventListener("DOMContentLoaded", () => init(document));
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = document.getElementById("drawer");
    if (event.detail.target === drawer) drawer.classList.add("open");
    updateConditional(event.detail.target.closest("form") || event.detail.target);
    setupTagInputs(event.detail.target);
    labelTables(event.detail.target.closest("table") || event.detail.target);
    updateRulePreviews(event.detail.target);
    document.querySelectorAll("[data-select-form]").forEach(updateSelection);
  });

  document.addEventListener("change", (event) => {
    const form = event.target.closest("form");
    if (form) {
      updateConditional(form);
      updateNewRuleFields(form);
    }
    if (event.target.matches("select[data-identifiers]")) updateRulePreviews(event.target.parentElement);
    const selectForm = event.target.closest("[data-select-form]");
    if (selectForm) {
      if (event.target.matches("[data-select-all]")) {
        selectForm.querySelectorAll('tbody input[type="checkbox"]').forEach((box) => {
          box.checked = event.target.checked;
        });
      }
      updateSelection(selectForm);
    }
    // "Apply to all rows" of the allow page
    const apply = event.target.closest("[data-apply]");
    if (apply && apply.value) {
      document.querySelectorAll("[data-row]").forEach((row) => {
        const field = row.querySelector(`[name$="-${apply.dataset.apply}"]`);
        if (!field) return;
        if (field.tagName === "SELECT" && ![...field.options].some((o) => o.value === apply.value)) return;
        field.value = apply.value;
      });
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

  document.addEventListener("click", (event) => {
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
      } else if (event.ctrlKey || event.metaKey) {
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
    updateSelection(form);
  });
  // no text selection while shift- or ctrl-clicking the rows
  document.addEventListener("mousedown", (event) => {
    if ((event.shiftKey || event.ctrlKey || event.metaKey) && event.target.closest("[data-select-form] tbody")
        && !event.target.closest(INTERACTIVE)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", (event) => {
    const burger = event.target.closest("[data-burger]");
    if (burger) {
      const open = burger.closest("[data-topbar]").classList.toggle("nav-open");
      burger.setAttribute("aria-expanded", open ? "true" : "false");
    } else if (!event.target.closest("#main-nav")) {
      document.querySelector("[data-topbar]")?.classList.remove("nav-open");
      document.querySelector("[data-burger]")?.setAttribute("aria-expanded", "false");
    }
    const menu = document.querySelector("[data-user-menu]");
    if (menu?.open && !event.target.closest("[data-user-menu]")) menu.open = false;
    if (event.target.closest("[data-drawer-close]")) closeDrawer();
    const copyButton = event.target.closest("[data-copy-text]");
    if (copyButton) copy(copyButton.dataset.copyText, copyButton);
    const confirmButton = event.target.closest("[data-confirm-when]");
    const clearButton = event.target.closest("[data-clear-selection]");
    if (clearButton) clearSelection(clearButton.closest("[data-select-form]"));
    if (confirmButton) {
      const [name, value] = confirmButton.dataset.confirmWhen.split("=");
      if (controlValue(confirmButton.form, name) === value && !window.confirm(confirmButton.dataset.confirm)) {
        event.preventDefault();
      }
    }
    if (!event.target.closest(".combobox")) {
      document.querySelectorAll(".suggestions.open").forEach((box) => box.classList.remove("open"));
    }
  });

  // an icon the catalog cannot deliver is left out, not shown as an empty picture
  document.addEventListener("error", (event) => {
    if (event.target.tagName === "IMG" && event.target.closest(".chip, .suggestion, .with-icon")) {
      event.target.remove();
    }
  }, true);

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const menu = document.querySelector("[data-user-menu]");
      if (menu?.open) {
        menu.open = false;
        menu.querySelector("summary").focus();
      }
      document.querySelector("[data-topbar]")?.classList.remove("nav-open");
    }
    if (event.key === "Escape") {
      if (document.getElementById("drawer")?.classList.contains("open")) {
        closeDrawer();
      } else if (!event.target.closest('input:not([type="checkbox"]), select, textarea')) {
        document.querySelectorAll("[data-select-form]").forEach((form) => {
          if (form.querySelector("[data-bulkbar].visible")) clearSelection(form);
        });
      }
    }
  });
})();
