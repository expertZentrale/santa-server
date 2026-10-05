// CEL expressions: suggestions while typing, and the expression of "only signing IDs starting with"
(function () {
  "use strict";

  const { moveSelection } = SantaConsole;

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
    // creating rules from events: the Team ID of the binaries, if they share one (else the server writes it per binary)
    const team = (identifier?.value || form.querySelector("[data-team-id]")?.dataset.teamId || "").trim();
    const values = prefixes.value.split("\n").map((line) => line.trim()).filter((line) => /^[A-Za-z0-9._-]+$/.test(line));
    expression.value = values.length && team
      ? `(${values.map((prefix) => `target.signing_id.startsWith("${team}:${prefix}")`).join(" || ")}) ? ALLOWLIST : BLOCKLIST`
      : "";
  });

  SantaConsole.onInit((root) => root.querySelectorAll("textarea[data-cel-suggestions]").forEach(setupCelInput));
})();
