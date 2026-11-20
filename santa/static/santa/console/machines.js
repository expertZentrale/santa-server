// The Macs of a rule or package rule as chips with a search: a suggestion or Enter adds one, × removes it.
// The hidden textarea keeps them one per line (serial numbers, or what was typed: the server checks it).
(function () {
  "use strict";

  const { debounce, moveSelection } = SantaConsole;

  function setupPicker(widget) {
    if (widget.dataset.pickerReady) return;
    widget.dataset.pickerReady = "1";
    const chips = widget.querySelector("[data-chips]");
    const search = widget.querySelector("[data-search]");
    const box = widget.querySelector("[data-suggestions]");
    const textarea = widget.querySelector("textarea");
    if (!chips || !search || !box || !textarea) return;
    let request = 0;

    function values() {
      return [...chips.children].map((chip) => chip.dataset.value);
    }

    function sync() {
      textarea.value = values().join("\n");
      textarea.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function setOpen(open) {
      box.classList.toggle("open", open);
      search.setAttribute("aria-expanded", String(open));
      if (!open) search.removeAttribute("aria-activedescendant");
    }

    function close() {
      request += 1;
      box.replaceChildren();
      setOpen(false);
    }

    function add(value, label, serial) {
      value = value.trim();
      search.value = "";
      close();
      if (!value || values().some((known) => known.toLowerCase() === value.toLowerCase())) return;
      const chip = document.createElement("li");
      chip.className = "chip";
      chip.dataset.value = value;
      const text = document.createElement("span");
      text.className = "chip-text";
      const strong = document.createElement("strong");
      strong.textContent = label || value;
      text.append(strong);
      if (serial && serial !== (label || value)) {
        const code = document.createElement("code");
        code.textContent = serial;
        text.append(code);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.setAttribute("aria-label", `${search.dataset.removeLabel || "Remove"} ${label || value}`);
      remove.textContent = "×";
      chip.append(text, remove);
      chips.append(chip);
      sync();
    }

    function choose(option) {
      add(option.dataset.value, option.dataset.label, option.dataset.value);
      search.focus();
    }

    const lookup = debounce(() => {
      const current = (request += 1);
      const q = search.value.trim();
      if (q.length < 2) {
        box.replaceChildren();
        setOpen(false);
        return;
      }
      fetch(`${widget.dataset.suggestUrl}?${new URLSearchParams({ q })}`,
            { headers: { "HX-Request": "true" }, credentials: "same-origin" })
        .then((response) => (response.ok ? response.text() : ""))
        .then((html) => {
          if (current !== request) return;
          // server-rendered, escaped template; the Macs already chosen are left out
          box.innerHTML = html;
          const chosen = new Set(values().map((value) => value.toLowerCase()));
          box.querySelectorAll(".suggestion").forEach((option) => {
            if (chosen.has(option.dataset.value.toLowerCase())) option.remove();
          });
          setOpen(document.activeElement === search && box.children.length > 0);
        })
        .catch(() => {
          if (current === request) setOpen(false);
        });
    }, 250);

    search.addEventListener("input", lookup);
    search.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!box.classList.contains("open")) return;
        event.preventDefault();
        moveSelection(box, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter") {
        // not the form: Enter adds the Mac
        event.preventDefault();
        const selected = box.classList.contains("open") && box.querySelector(".suggestion[aria-selected=true]");
        const only = box.classList.contains("open") && box.querySelectorAll(".suggestion").length === 1
          && box.querySelector(".suggestion");
        if (selected || only) choose(selected || only);
        else add(search.value);
      } else if (event.key === "Escape" && box.classList.contains("open")) {
        // only the suggestions close, not the drawer around the form
        event.stopPropagation();
        close();
      } else if (event.key === "Backspace" && !search.value && chips.lastElementChild) {
        chips.lastElementChild.remove();
        sync();
      }
    });
    // mousedown: before the input loses the focus
    box.addEventListener("mousedown", (event) => {
      const option = event.target.closest(".suggestion");
      if (option) {
        event.preventDefault();
        choose(option);
      }
    });
    search.addEventListener("blur", () => setTimeout(() => setOpen(false), 150));
    chips.addEventListener("click", (event) => {
      const remove = event.target.closest(".chip-remove");
      if (!remove) return;
      remove.closest(".chip").remove();
      sync();
      search.focus();
    });
    // a Mac typed but not confirmed with Enter counts too; capture: before htmx reads the form
    const form = widget.closest("form");
    form?.addEventListener("submit", () => { if (search.value.trim()) add(search.value); }, true);
    form?.addEventListener("htmx:configRequest", (event) => {
      if (!search.value.trim()) return;
      add(search.value);
      event.detail.parameters[textarea.name] = textarea.value;
    }, true);
  }

  function setup(root) {
    root.querySelectorAll("[data-machine-picker]").forEach(setupPicker);
  }

  SantaConsole.onInit(setup);
  SantaConsole.onSwap(setup);
})();
