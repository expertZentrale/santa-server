// Patterns as chips (the version, asset and binary patterns of a package rule): Enter adds one, × removes it.
// The hidden textarea keeps them one per line, as the server stores them.
(function () {
  "use strict";

  function setupChips(widget) {
    if (widget.dataset.chipsReady) return;
    widget.dataset.chipsReady = "1";
    const chips = widget.querySelector("[data-chips]");
    const input = widget.querySelector("[data-chips-input]");
    const textarea = widget.querySelector("textarea");
    if (!chips || !input || !textarea) return;

    function values() {
      return [...chips.children].map((chip) => chip.dataset.value);
    }

    function sync() {
      textarea.value = values().join("\n");
    }

    function add(value) {
      value = value.trim();
      input.value = "";
      if (!value || values().includes(value)) return;
      const chip = document.createElement("li");
      chip.className = "chip";
      chip.dataset.value = value;
      const text = document.createElement("code");
      text.className = "chip-text";
      text.textContent = value;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.setAttribute("aria-label", `${input.dataset.removeLabel || "Remove"} ${value}`);
      remove.textContent = "×";
      chip.append(text, remove);
      chips.append(chip);
      sync();
    }

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        // not the form: Enter only adds the pattern
        event.preventDefault();
        add(input.value);
      } else if (event.key === "Backspace" && !input.value && chips.lastElementChild) {
        chips.lastElementChild.remove();
        sync();
      }
    });
    chips.addEventListener("click", (event) => {
      const remove = event.target.closest(".chip-remove");
      if (!remove) return;
      remove.closest(".chip").remove();
      sync();
      input.focus();
    });
    // a pattern typed but not confirmed with Enter counts too; capture: before htmx reads the form
    widget.closest("form")?.addEventListener("submit", () => add(input.value), true);
    // the preview (a button with hx-post): htmx has read the form already, the parameter gets the typed one too
    widget.closest("form")?.addEventListener("htmx:configRequest", (event) => {
      if (!input.value.trim()) return;
      add(input.value);
      event.detail.parameters[textarea.name] = textarea.value;
    }, true);
  }

  function setup(root) {
    root.querySelectorAll("[data-chips-widget]").forEach(setupChips);
  }

  SantaConsole.onInit(setup);
  SantaConsole.onSwap(setup);
})();
