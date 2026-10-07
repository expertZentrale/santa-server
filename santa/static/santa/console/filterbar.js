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
          // the date range waits for "Apply"
          if (!event.target.hasAttribute("data-range")) submit();
          return;
        }
        if (event.target.type === "checkbox") changed = true;
      });
      // closed without a change, a chip added with "+ Filter" stays ("all"); it goes with the next search
      chip.addEventListener("toggle", () => {
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
      const chip = form.querySelector(`.filter-chip[data-facet="${item.dataset.openFacet}"]`);
      if (!chip) return;
      item.closest("details").open = false;
      item.hidden = true;
      chip.hidden = false;
      chip.querySelector("fieldset")?.removeAttribute("disabled");
      // after the click: the document closes the pickers the click was not in
      requestAnimationFrame(() => {
        const details = chip.querySelector("details");
        details.open = true;
        details.querySelector("input")?.focus();
      });
    });
  }

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
})();
