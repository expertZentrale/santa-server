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

  // The drawer: details and forms. A link inside it opens the next view in it; the views before are kept
  // (their DOM, with what was entered) and "Back" brings them back.
  const drawerStack = [];
  let drawerSaved = false;

  function closeDrawer() {
    const drawer = document.getElementById("drawer");
    // the drawer saved something the page behind it doesn't show yet
    if (drawerSaved || drawer.querySelector("[data-refresh-on-close]")) {
      window.location.reload();
      return;
    }
    drawerStack.length = 0;
    drawer.classList.remove("open");
    drawer.replaceChildren();
  }

  function pushDrawerView(drawer) {
    const view = document.createDocumentFragment();
    const scroll = drawer.scrollTop;
    while (drawer.firstChild) view.append(drawer.firstChild);
    drawerStack.push({ view, scroll });
  }

  function updateDrawerBack(drawer) {
    drawer.querySelectorAll("[data-drawer-back]").forEach((button) => button.remove());
    // the links back of the templates are for the page, the stack knows the way back
    drawer.querySelectorAll(".drawer-head .back").forEach((link) => {
      link.hidden = drawerStack.length > 0;
    });
    const tools = drawer.querySelector(".drawer-tools");
    const template = document.getElementById("drawer-back");
    if (drawerStack.length && tools && template) tools.prepend(template.content.cloneNode(true));
  }

  function drawerBack() {
    const drawer = document.getElementById("drawer");
    const previous = drawerStack.pop();
    if (!previous) return;
    drawer.replaceChildren(previous.view);
    drawer.scrollTop = previous.scroll;
    updateDrawerBack(drawer);
    drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
  }

  // a view of the drawer again from the server, after something was saved in the view after it
  function reloadDrawerView(drawer) {
    const createRules = drawer.querySelector("[data-create-rules]");
    if (createRules) {
      htmx.ajax("POST", createRules.getAttribute("action"), {
        source: createRules, target: "#drawer", values: { refresh: "1" }, headers: { "X-Drawer-Reload": "1" },
      });
      return;
    }
    const url = drawer.querySelector("[data-drawer-url]")?.dataset.drawerUrl;
    if (url) htmx.ajax("GET", url, { target: "#drawer", headers: { "X-Drawer-Reload": "1" } });
  }

  document.addEventListener("htmx:beforeSwap", (event) => {
    const drawer = document.getElementById("drawer");
    if (event.detail.target !== drawer || !event.detail.shouldSwap) return;
    const config = event.detail.requestConfig || {};
    if (config.headers?.["X-Drawer-Reload"]) return;
    const inside = config.elt && config.elt !== drawer && drawer.contains(config.elt);
    if (config.verb === "get" && inside && drawer.classList.contains("open")) {
      pushDrawerView(drawer);
    } else if (!inside) {
      // opened from the page: a new start
      drawerStack.length = 0;
    }
  });

  // drawer_done(): back to the view before and update it, or reload the page when there is none
  document.addEventListener("drawerSaved", () => {
    const drawer = document.getElementById("drawer");
    drawerSaved = true;
    if (!drawerStack.length) {
      window.location.reload();
      return;
    }
    drawerBack();
    reloadDrawerView(drawer);
  });

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
        // only open suggestions close, not the drawer around the form
        if (box.classList.contains("open")) event.stopPropagation();
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
        // only open suggestions close, not the drawer around the form
        if (box.classList.contains("open")) event.stopPropagation();
        box.classList.remove("open");
      }
    });
    box.addEventListener("click", (event) => {
      const option = event.target.closest(".suggestion");
      // already allowed on the Mac of the user
      if (!option || option.getAttribute("aria-disabled") === "true") return;
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
    const options = [...box.querySelectorAll(".suggestion:not([aria-disabled=true])")];
    if (!options.length) return;
    let index = options.findIndex((option) => option.getAttribute("aria-selected") === "true");
    options.forEach((option) => option.setAttribute("aria-selected", "false"));
    index = Math.min(Math.max(index + step, 0), options.length - 1);
    options[index].setAttribute("aria-selected", "true");
    options[index].scrollIntoView({ block: "nearest" });
    // screen readers follow the option through the input that controls the list
    if (box.id && options[index].id) {
      document.querySelector(`[aria-controls="${box.id}"]`)?.setAttribute("aria-activedescendant", options[index].id);
    }
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
        // only open suggestions close, not the drawer around the form
        if (box.classList.contains("open")) event.stopPropagation();
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

  // Tags as chips in one input: an existing tag selects its option in the (hidden) multiple select,
  // a new name goes into the new_tags field. Without JS both fields stay as they are.
  function setupTagPicker(select) {
    const form = select.closest("form");
    const newInput = form?.querySelector(`input[name="${select.name.replace(/tags$/, "new_tags")}"]`);
    if (!newInput || select.dataset.tagPicker) return;
    select.dataset.tagPicker = "1";
    newInput.dataset.tagInput = "1";
    const field = select.closest(".field");
    const newField = newInput.closest(".field");
    const chips = document.createElement("ul");
    chips.className = "chips";
    const wrapper = document.createElement("div");
    wrapper.className = "combobox";
    const search = document.createElement("input");
    search.type = "text";
    search.id = `${select.id}-search`;
    search.autocomplete = "off";
    search.placeholder = select.dataset.placeholder || "";
    search.setAttribute("role", "combobox");
    search.setAttribute("aria-autocomplete", "list");
    search.setAttribute("aria-expanded", "false");
    const box = document.createElement("div");
    box.className = "suggestions";
    box.id = `${select.id}-suggestions`;
    box.setAttribute("role", "listbox");
    search.setAttribute("aria-controls", box.id);
    wrapper.append(search, box);
    select.hidden = true;
    select.after(chips, wrapper);
    field.querySelector(`label[for="${select.id}"]`)?.setAttribute("for", search.id);
    // the new_tags value is submitted from the hidden input, its field is not needed anymore
    newInput.type = "hidden";
    field.append(newInput);
    if (newField && newField !== field) {
      newField.querySelectorAll(".error").forEach((error) => field.append(error));
      newField.hidden = true;
    }
    const options = [...select.options];
    let added = newInput.value.split(",").map((name) => name.trim()).filter(Boolean);

    function render() {
      const items = [...options.filter((option) => option.selected).map((option) => ({ name: option.text, option })),
                     ...added.map((name) => ({ name }))];
      chips.replaceChildren(...items.map((item) => {
        const chip = document.createElement("li");
        chip.className = "chip";
        const text = document.createElement("span");
        text.textContent = item.name;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "chip-remove";
        remove.setAttribute("aria-label", `${select.dataset.removeLabel || "Remove"} ${item.name}`);
        remove.textContent = "×";
        remove.addEventListener("click", () => {
          if (item.option) item.option.selected = false;
          else added = added.filter((name) => name !== item.name);
          render();
          search.focus();
        });
        chip.append(text, remove);
        return chip;
      }));
      newInput.value = added.join(", ");
    }

    function show() {
      const typed = search.value.trim().toLowerCase();
      const matches = options.filter((option) => !option.selected && option.text.toLowerCase().includes(typed))
                             .slice(0, 8);
      box.replaceChildren(...matches.map((option, index) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "suggestion tag-suggestion";
        item.id = `${box.id}-${index}`;
        item.setAttribute("role", "option");
        item.dataset.tag = option.text;
        item.textContent = option.text;
        return item;
      }));
      const open = matches.length > 0 && document.activeElement === search;
      box.classList.toggle("open", open);
      search.setAttribute("aria-expanded", open ? "true" : "false");
      search.removeAttribute("aria-activedescendant");
    }

    function add(name) {
      name = name.trim().slice(0, 100);
      if (!name) return;
      const option = options.find((item) => item.text.toLowerCase() === name.toLowerCase());
      if (option) option.selected = true;
      else if (!added.some((item) => item.toLowerCase() === name.toLowerCase())) added.push(name);
      search.value = "";
      render();
      show();
    }

    search.addEventListener("input", () => {
      if (search.value.includes(",")) {
        search.value.split(",").forEach(add);
      } else {
        show();
      }
    });
    search.addEventListener("focus", show);
    search.addEventListener("keydown", (event) => {
      const open = box.classList.contains("open");
      if (open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter") {
        const selected = open && box.querySelector(".suggestion[aria-selected=true]");
        if (selected || search.value.trim()) {
          event.preventDefault();
          add(selected ? selected.dataset.tag : search.value);
        }
      } else if (event.key === "Backspace" && !search.value) {
        const last = added.length ? null : options.filter((option) => option.selected).pop();
        if (added.length) added.pop();
        else if (last) last.selected = false;
        render();
      } else if (event.key === "Escape" && open) {
        event.preventDefault();
        event.stopPropagation();
        box.classList.remove("open");
      }
    });
    // mousedown: before the input loses the focus
    box.addEventListener("mousedown", (event) => {
      const item = event.target.closest(".suggestion");
      if (item) {
        event.preventDefault();
        add(item.dataset.tag);
      }
    });
    // a typed name counts, also without Enter (the suggestions keep the focus with preventDefault)
    search.addEventListener("blur", () => {
      if (search.value.trim()) add(search.value);
      box.classList.remove("open");
      search.setAttribute("aria-expanded", "false");
    });
    render();
  }

  function setupTagInputs(root) {
    root.querySelectorAll('select[multiple][name$="tags"]').forEach(setupTagPicker);
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

  // Rules for several binaries: what each rule uses with the chosen rule type, or its suggestion when the binary
  // has no identifier of that type
  function updateBinaryPreviews(root) {
    root.querySelectorAll("[data-binaries]").forEach((list) => {
      const select = list.closest("form").querySelector('select[name$="rule_type"]');
      let labels = {};
      try {
        labels = JSON.parse(list.dataset.ruleTypes);
      } catch (error) {
        return;
      }
      list.querySelectorAll("[data-binary-preview]").forEach((preview) => {
        let identifiers = {};
        try {
          identifiers = JSON.parse(preview.dataset.identifiers);
        } catch (error) {
          return;
        }
        const chosen = select?.value || "";
        const fallback = chosen && !identifiers[chosen];
        const type = chosen && !fallback ? chosen : preview.dataset.suggested;
        const code = document.createElement("code");
        code.textContent = identifiers[type] || "";
        preview.classList.toggle("fallback", Boolean(fallback));
        preview.replaceChildren(`${fallback ? list.dataset.fallbackLabel : list.dataset.previewLabel} ${labels[type] || type}: `,
                                code);
      });
    });
  }

  function init(root) {
    labelTables(root);
    setupTables(root);
    setupRowFocus(root);
    root.querySelectorAll("textarea[data-cel-suggestions]").forEach(setupCelInput);
    updateRulePreviews(root);
    updateBinaryPreviews(root);
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
    // IANA names (Europe/Berlin) are valid cookie values as they are; Django doesn't URL-decode cookies
    if (!/^[A-Za-z0-9_+\-/]+$/.test(zone)) return;
    const current = document.cookie.split("; ").find((item) => item.startsWith("santa_tz="));
    if (current === `santa_tz=${zone}`) return;
    const secure = location.protocol === "https:" ? "; secure" : "";
    document.cookie = `santa_tz=${zone}; path=/; max-age=31536000; samesite=lax${secure}`;
  }

  // the checkbox pickers (details): the count on the button, closed by a click outside or Escape
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
    // only the picker closes, not the drawer or the selection
    event.stopImmediatePropagation();
    open.open = false;
    open.querySelector("summary").focus();
  });

  // New events without a reload: every 10 s the changed rows come from the server. A row that is already in the
  // list is replaced (its checkbox stays as it is), a new one is added on top (first page, default order). The
  // pill in the top bar counts the new rows not seen yet, the selection and an open drawer are not touched.
  function setupLiveUpdates() {
    const table = document.querySelector("table[data-live-updates]");
    const pill = document.querySelector("[data-live-pill]");
    if (!table || !pill) return;
    const tbody = table.tBodies[0];
    const baseTitle = document.title;
    const unseen = new Set();
    let elsewhere = 0;
    let after = table.dataset.after;
    const seen = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        unseen.delete(entry.target);
        seen.unobserve(entry.target);
      });
      renderPill();
    }, {
      // behind the sticky top bar is not seen, and most of the row must be visible
      rootMargin: `-${document.querySelector(".topbar")?.offsetHeight || 0}px 0px 0px 0px`, threshold: 0.6,
    });

    function renderPill() {
      const count = unseen.size + elsewhere;
      pill.hidden = !count;
      pill.querySelector("[data-live-pill-count]").textContent = count;
      pill.querySelector("[data-live-pill-text]").textContent =
        ` ${count === 1 ? pill.dataset.one : pill.dataset.many}${unseen.size ? "" : ` ${pill.dataset.elsewhere}`}`;
      document.title = count ? `(${count}) ${baseTitle}` : baseTitle;
    }

    function apply(rows) {
      rows.forEach((row) => {
        const current = [...tbody.rows].find((other) => other.dataset.key === row.dataset.key);
        const box = current?.querySelector('td.select input[type="checkbox"]');
        if (box?.checked) row.querySelector('td.select input[type="checkbox"]').checked = true;
        if (current && !("insert" in table.dataset)) {
          current.replaceWith(row);
        } else if ("insert" in table.dataset) {
          current?.remove();
          tbody.querySelector("tr.empty")?.remove();
          tbody.prepend(row);
        } else {
          elsewhere += 1;
          return;
        }
        row.classList.add("new");
        unseen.add(row);
        seen.observe(row);
      });
      htmx.process(tbody);
      labelTables(table.closest(".table-scroll") || table);
      setupRowFocus(document);
      document.querySelectorAll("[data-select-form]").forEach(updateSelection);
      renderPill();
    }

    async function poll() {
      if (document.hidden) return;
      const url = new URL(table.dataset.liveUpdates, window.location.href);
      url.searchParams.set("after", after);
      try {
        const response = await fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" });
        if (response.status !== 200) return;
        const wrapper = document.createElement("template");
        wrapper.innerHTML = await response.text();
        const update = wrapper.content.querySelector("template[data-max-pk]");
        if (!update) return;
        after = update.dataset.maxPk;
        // newest last, so that prepending keeps the newest on top
        apply([...update.content.querySelectorAll("tr[data-key]")].reverse());
      } catch (error) {
        // offline for a moment: the next poll tries again
      }
    }

    pill.addEventListener("click", (event) => {
      const first = [...tbody.rows].find((row) => unseen.has(row));
      if (!first) return;
      event.preventDefault();
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      first.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    });
    setInterval(poll, 10000);
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

  const rem = () => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;

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

  function setupTable(table) {
    if (table.dataset.tableReady || !table.tHead) return;
    table.dataset.tableReady = "1";
    const texts = document.getElementById("table-texts")?.dataset || {};
    const settings = tableSettings(table);
    [...table.tHead.rows[0].cells].forEach((th) => {
      if (!th.dataset.col && !th.classList.contains("select")) {
        th.dataset.col = th.textContent.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-") || `col${th.cellIndex}`;
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
        settings.widths = { ...(settings.widths || {}), [key]: Math.max(3, Math.round(width * 10) / 10) };
        applyWidths(table, settings);
        updateFits(table);
      };
      const reset = () => {
        if (settings.widths) delete settings.widths[key];
        applyWidths(table, settings);
        updateFits(table);
        saveTableSettings(table, settings);
      };
      handle.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); });
      handle.addEventListener("dblclick", reset);
      handle.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          const step = (event.shiftKey ? 2 : 0.5) * (event.key === "ArrowLeft" ? -1 : 1);
          setWidth(th.getBoundingClientRect().width / rem() + step);
          saveTableSettings(table, settings);
        } else if (event.key === "Enter") {
          event.preventDefault();
          reset();
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

    // the column chooser: in the filter row, or above the table
    const picker = document.createElement("details");
    picker.className = "picker down columns-picker";
    picker.dataset.picker = "";
    const summary = document.createElement("summary");
    summary.className = "button";
    summary.textContent = texts.columns || "Columns";
    const panel = document.createElement("div");
    panel.className = "picker-panel";
    const hidden = applyHidden(table, settings);
    tableColumns(table).forEach(({ th, key }) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !hidden.has(key);
      box.dataset.columnKey = key;
      label.append(box, ` ${th.textContent.trim()}`);
      panel.append(label);
    });
    const resetAll = document.createElement("button");
    resetAll.type = "button";
    resetAll.className = "button small";
    resetAll.textContent = texts.reset || "Reset widths and columns";
    panel.append(resetAll);
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
    const filters = document.querySelector("main form.filters");
    if (filters) {
      picker.classList.add("right");
      filters.append(picker);
    } else {
      const tools = document.createElement("div");
      tools.className = "table-tools";
      tools.append(picker);
      (table.closest(".table-scroll") || table).before(tools);
    }
    updateFits(table);
  }

  function setupTables(root) {
    root.querySelectorAll("table[data-table]").forEach(setupTable);
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

  // CEL expressions: suggestions for the known fields, functions and results while typing. They only complete,
  // anything else can be typed (newer Santa versions know more).
  function setupCelInput(textarea) {
    if (textarea.dataset.celReady) return;
    textarea.dataset.celReady = "1";
    let items = [];
    try {
      items = JSON.parse(textarea.dataset.celSuggestions);
    } catch (error) {
      return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "combobox";
    textarea.replaceWith(wrapper);
    const box = document.createElement("div");
    box.className = "suggestions";
    box.id = `${textarea.id || "cel"}-suggestions`;
    box.setAttribute("role", "listbox");
    wrapper.append(textarea, box);
    textarea.setAttribute("autocomplete", "off");
    textarea.setAttribute("role", "combobox");
    textarea.setAttribute("aria-autocomplete", "list");
    textarea.setAttribute("aria-controls", box.id);
    textarea.setAttribute("aria-expanded", "false");
    textarea.spellcheck = false;
    let match = null;

    function setOpen(open) {
      box.classList.toggle("open", open);
      textarea.setAttribute("aria-expanded", open ? "true" : "false");
      const selected = open && box.querySelector(".suggestion[aria-selected=true]");
      if (selected) textarea.setAttribute("aria-activedescendant", selected.id);
      else textarea.removeAttribute("aria-activedescendant");
    }

    function current() {
      const before = textarea.value.slice(0, textarea.selectionStart);
      const token = before.match(/[A-Za-z_][A-Za-z0-9_.]*$/)?.[0] || "";
      return { token, last: token.includes(".") ? token.slice(token.lastIndexOf(".") + 1) : token };
    }

    function show() {
      const { token, last } = current();
      if (!token) {
        setOpen(false);
        return;
      }
      const lower = token.toLowerCase();
      const found = items.filter((item) => {
        const text = item.text.trim().toLowerCase();
        // functions follow a value: target.signing_id.sta… → startsWith(""), but not target.s…
        if (item.kind === "function") {
          const owner = token.includes(".") ? token.slice(0, token.lastIndexOf(".")) : "";
          return owner && !["target", "ancestors"].includes(owner) && last
                 && text.startsWith(last.toLowerCase()) && text !== last.toLowerCase();
        }
        // global functions (timestamp, duration) start a value, like the fields
        if (item.kind === "global" && token.includes(".")) return false;
        return text.startsWith(lower) && text !== lower;
      }).slice(0, 8);
      match = { token, last };
      box.replaceChildren(...found.map((item, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "suggestion cel-suggestion";
        option.id = `${box.id}-${index}`;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", index === 0 ? "true" : "false");
        option.dataset.text = item.text;
        option.dataset.kind = item.kind;
        const text = document.createElement("span");
        const code = document.createElement("code");
        code.textContent = item.text.trim();
        const help = document.createElement("small");
        help.textContent = item.help;
        text.append(code, help);
        option.append(text);
        return option;
      }));
      setOpen(found.length > 0);
      // in the drawer the list can end below the window
      if (found.length) box.scrollIntoView({ block: "nearest" });
    }

    function insert(option) {
      const replaced = option.dataset.kind === "function" ? match.last : match.token;
      const end = textarea.selectionStart;
      const start = end - replaced.length;
      const text = option.dataset.text;
      textarea.setRangeText(text, start, end, "end");
      // the cursor goes into the quotes, the brackets or the parentheses
      const inner = text.search(/""|\(\)|\[\]|, \)/);
      if (inner >= 0) {
        const offset = text.slice(inner, inner + 2) === ", " ? inner + 2 : inner + 1;
        textarea.selectionStart = textarea.selectionEnd = start + offset;
      }
      setOpen(false);
      textarea.focus();
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }

    textarea.addEventListener("input", show);
    textarea.addEventListener("click", show);
    textarea.addEventListener("keydown", (event) => {
      if (!box.classList.contains("open")) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter" || event.key === "Tab") {
        const selected = box.querySelector(".suggestion[aria-selected=true]");
        if (selected) {
          event.preventDefault();
          insert(selected);
        }
      } else if (event.key === "Escape") {
        event.stopPropagation();
        setOpen(false);
      }
    });
    box.addEventListener("mousedown", (event) => {
      const option = event.target.closest(".suggestion");
      if (option) {
        event.preventDefault();
        insert(option);
      }
    });
    textarea.addEventListener("blur", () => setTimeout(() => setOpen(false), 150));
  }

  // "Only signing IDs starting with": writes the CEL expression of the Team ID rule, the same as the server
  // (forms.prefix_expression); an expression written by hand is never replaced
  const PREFIX_EXPRESSION = /^\(target\.signing_id\.startsWith\("[A-Z0-9]{10}:[A-Za-z0-9._-]+"\)( \|\| target\.signing_id\.startsWith\("[A-Z0-9]{10}:[A-Za-z0-9._-]+"\))*\) \? ALLOWLIST : BLOCKLIST$/;

  document.addEventListener("input", (event) => {
    const prefixes = event.target.closest?.('textarea[name$="signing_prefixes"]');
    if (!prefixes) return;
    const form = prefixes.closest("form");
    const expression = form.querySelector('textarea[name$="cel_expr"]');
    const identifier = form.querySelector('input[name$="identifier"]');
    if (!expression) return;
    if (expression.value.trim() && !PREFIX_EXPRESSION.test(expression.value.trim())) return;
    const team = (identifier?.value || "").trim();
    const values = prefixes.value.split("\n").map((line) => line.trim()).filter((line) => /^[A-Za-z0-9._-]+$/.test(line));
    expression.value = values.length && team
      ? `(${values.map((prefix) => `target.signing_id.startsWith("${team}:${prefix}")`).join(" || ")}) ? ALLOWLIST : BLOCKLIST`
      : "";
  });

  sendTimeZone();
  document.addEventListener("DOMContentLoaded", setupLiveUpdates);
  document.addEventListener("DOMContentLoaded", () => init(document));
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = document.getElementById("drawer");
    if (event.detail.target === drawer) {
      drawer.classList.add("open");
      init(drawer);
      uncheckSaved(drawer);
      updateDrawerBack(drawer);
      drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
    }
    updateConditional(event.detail.target.closest("form") || event.detail.target);
    setupTagInputs(event.detail.target);
    labelTables(event.detail.target.closest("table") || event.detail.target);
    updateRulePreviews(event.detail.target);
    setupRowFocus(document);
    document.querySelectorAll("[data-select-form]").forEach(updateSelection);
  });

  document.addEventListener("change", (event) => {
    const form = event.target.closest("form");
    if (form) {
      updateConditional(form);
      updateNewRuleFields(form);
    }
    if (event.target.matches("select[data-identifiers]")) updateRulePreviews(event.target.parentElement);
    if (form && event.target.matches('select[name$="rule_type"]')) updateBinaryPreviews(form);
    const selectForm = event.target.closest("[data-select-form]");
    if (selectForm) {
      if (event.target.matches("[data-select-all]")) {
        selectForm.querySelectorAll('tbody input[type="checkbox"]').forEach((box) => {
          box.checked = event.target.checked;
        });
      }
      updateSelection(selectForm);
      syncCreateRules(selectForm);
    }
  });

  // The drawer "create rules" follows the selection of the list behind it: a click, Ctrl- or Shift-click adds or
  // removes binaries. The drawer form is posted with ?refresh, which never saves and keeps what was entered.
  let syncTimer = null;

  function syncCreateRules(listForm) {
    const drawer = document.getElementById("drawer");
    const drawerForm = drawer.querySelector("[data-create-rules]");
    if (!drawerForm || !drawer.classList.contains("open") || drawer.contains(listForm)) return;
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => {
      drawerForm.querySelectorAll('input[type="hidden"][name="ids"], input[type="hidden"][name="shas"]')
                .forEach((input) => input.remove());
      rowBoxes(listForm).filter((box) => box.checked).forEach((box) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = box.name;
        input.value = box.value;
        drawerForm.append(input);
      });
      const resolved = listForm.querySelector('[name="resolved"]');
      const drawerResolved = drawerForm.querySelector('[name="resolved"]');
      if (resolved && drawerResolved) drawerResolved.value = resolved.value;
      const scroll = drawer.scrollTop;
      htmx.ajax("POST", drawerForm.getAttribute("action"), {
        source: drawerForm, target: "#drawer", values: { refresh: "1" },
      }).then(() => {
        drawer.scrollTop = scroll;
      });
    }, 250);
  }

  // after saving with binaries left: the saved ones are no longer selected in the list
  function uncheckSaved(drawer) {
    const saved = drawer.querySelector("[data-saved-shas]");
    if (!saved) return;
    const shas = new Set(saved.dataset.savedShas.split(" "));
    document.querySelectorAll("[data-select-form]").forEach((form) => {
      if (drawer.contains(form)) return;
      rowBoxes(form).forEach((box) => {
        if (shas.has(box.dataset.sha || box.value)) box.checked = false;
      });
      updateSelection(form);
    });
  }

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

  // the whole cell of a switch toggles it, not only the small switch (else the click would select the row)
  document.addEventListener("click", (event) => {
    const cell = event.target.closest("td.toggle");
    if (!cell || event.target.closest(INTERACTIVE)) return;
    const toggle = cell.querySelector(".switch");
    if (!toggle) return;
    event.stopImmediatePropagation();
    toggle.click();
  }, true);

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
    syncCreateRules(form);
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
    // × closes the drawer; "Cancel" of a view opened from another one goes back to it
    const close = event.target.closest("[data-drawer-close]");
    if (close && drawerStack.length && !close.closest(".drawer-tools")) drawerBack();
    else if (close) closeDrawer();
    if (event.target.closest("[data-drawer-back]")) drawerBack();
    // a click next to the open drawer closes it, except on what opens another one
    const drawer = document.getElementById("drawer");
    // "create rules" follows the selection: selecting rows of the list keeps it open
    const selecting = drawer?.querySelector("[data-create-rules]")
                      && event.target.closest("[data-select-form] tbody, [data-select-all]");
    if (drawer?.classList.contains("open") && event.target.isConnected && !event.target.closest("#drawer")
        && !event.target.closest('[hx-target="#drawer"]') && !selecting) {
      closeDrawer();
    }
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
