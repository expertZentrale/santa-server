// Tags: typed as "a, b" with suggestions, or as chips in one input (the tag picker)
(function () {
  "use strict";

  const { moveSelection } = SantaConsole;

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

  SantaConsole.onInit(setupTagInputs);
  SantaConsole.onSwap(setupTagInputs);
})();
