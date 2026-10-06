// Catalog suggestions: the identifiers widget of the package rules and the packages of the request form
(function () {
  "use strict";

  const { debounce, moveSelection } = SantaConsole;

  // the latest request of each box: an older answer that arrives later is dropped
  const latest = new WeakMap();

  function nextRequest(box) {
    const request = (latest.get(box) || 0) + 1;
    latest.set(box, request);
    return request;
  }

  // closed without a search: a request still under way doesn't open it again
  function closeSuggestions(box) {
    nextRequest(box);
    box.replaceChildren();
    box.classList.remove("open");
  }

  // params: the search text as q, e.g. { kind, q }; setOpen shows or hides the box
  function fetchSuggestions(url, params, box, setOpen = (open) => box.classList.toggle("open", open)) {
    const request = nextRequest(box);
    if (params.q.trim().length < 2) {
      box.replaceChildren();
      setOpen(false);
      return;
    }
    fetch(`${url}?${new URLSearchParams(params)}`, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
      .then((response) => (response.ok ? response.text() : ""))
      .then((html) => {
        if (latest.get(box) !== request) return;
        // server-rendered, escaped template
        box.innerHTML = html;
        setOpen(html.trim() !== "");
      })
      .catch(() => {
        if (latest.get(box) === request) setOpen(false);
      });
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
        closeSuggestions(box);
        return;
      }
      fetchSuggestions(form.dataset.catalogUrl, { kind, q: search.value }, box);
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
      if (searchable.includes(kind.value)) fetchSuggestions(form.dataset.catalogUrl, { kind: kind.value, q: search.value }, box);
      else closeSuggestions(box);
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

  // "Existing execution rule" of a request: the rules for the text typed; a pick also sets the hidden rule (one
  // identifier can have rules for several scopes)
  function setupRuleSearch(field) {
    if (field.dataset.ruleSearchReady) return;
    field.dataset.ruleSearchReady = "1";
    const input = field.querySelector("input");
    const box = field.querySelector("[data-suggestions]");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", box.id);
    input.setAttribute("aria-expanded", "false");
    const hidden = field.closest("form")?.querySelector('input[name="rule"]');
    const setOpen = (open) => {
      box.classList.toggle("open", open);
      input.setAttribute("aria-expanded", String(open));
      if (!open) input.removeAttribute("aria-activedescendant");
    };
    const lookup = debounce(() => fetchSuggestions(field.dataset.url, { q: input.value }, box, setOpen), 300);
    const pick = (option) => {
      input.value = option.dataset.identifier;
      if (hidden) hidden.value = option.dataset.rule;
      setOpen(false);
      input.focus();
    };
    input.addEventListener("input", () => {
      // typed by hand: the rule is looked up by its identifier
      if (hidden) hidden.value = "";
      lookup();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter" && box.classList.contains("open")) {
        const selected = box.querySelector(".suggestion[aria-selected=true]");
        if (selected) {
          event.preventDefault();
          pick(selected);
        }
      } else if (event.key === "Escape" && box.classList.contains("open")) {
        // only the suggestions close, not the drawer around the form
        event.stopPropagation();
        setOpen(false);
      }
    });
    box.addEventListener("click", (event) => {
      const option = event.target.closest(".suggestion");
      if (option) pick(option);
    });
  }

  SantaConsole.onInit((root) => {
    root.querySelectorAll("[data-source-form]").forEach(setupIdentifiers);
    root.querySelectorAll("[data-package-form]").forEach(setupPackageRequest);
    root.querySelectorAll("[data-rule-search]").forEach(setupRuleSearch);
  });

  // a click outside closes the open suggestions (of every combobox)
  document.addEventListener("click", (event) => {
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
})();
