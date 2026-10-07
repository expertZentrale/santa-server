// Forms: fields shown depending on other fields, previews of the rule identifiers, confirmations, and the drawer
// "create rules" that follows the selection of the list behind it
(function () {
  "use strict";

  const { controlValue } = SantaConsole;

  // data-show-when="policy=BLOCKLIST|CEL", data-show-when="parent" (not empty), data-hide-when="is_global"
  function updateConditional(root) {
    root.querySelectorAll("[data-show-when], [data-hide-when]").forEach((element) => {
      const form = element.closest("form") || document;
      let visible = true;
      if (element.dataset.showWhen) {
        const [name, values] = element.dataset.showWhen.split("=");
        visible = values === undefined ? Boolean(controlValue(form, name))
          : values.split("|").includes(controlValue(form, name));
      }
      if (element.dataset.hideWhen) {
        visible = visible && !controlValue(form, element.dataset.hideWhen);
      }
      element.hidden = !visible;
    });
  }

  // A group based on another one: per setting the inherited value, or the own one ("Own value" ticked)
  function updateInherited(root) {
    root.querySelectorAll("[data-setting]").forEach((setting) => {
      const form = setting.closest("form");
      const parent = controlValue(form, "parent");
      const override = setting.querySelector("[data-inherit-override]");
      const own = !parent || Boolean(controlValue(override, override.querySelector("input").name));
      override.hidden = !parent;
      setting.querySelector("[data-own]").hidden = !own;
      setting.querySelector("[data-inherited]").hidden = own;
      // the values shown are the ones of the saved parent
      const saved = parent === setting.dataset.savedParent;
      setting.querySelector("[data-inherited-values]").hidden = !saved;
      setting.querySelector("[data-inherited-pending]").hidden = saved;
      const from = setting.querySelector("[data-inherited-from]");
      if (from) from.hidden = !saved;
    });
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
      SantaConsole.table.rowBoxes(listForm).filter((box) => box.checked).forEach((box) => {
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
      SantaConsole.table.rowBoxes(form).forEach((box) => {
        if (shas.has(box.dataset.sha || box.value)) box.checked = false;
      });
      SantaConsole.table.updateSelection(form);
    });
  }

  // Formsets (data-formset="<prefix>"): "add" copies the <template> of an empty form, with the next index
  document.addEventListener("click", (event) => {
    const add = event.target.closest("[data-formset-add]");
    if (!add) return;
    const container = add.closest("[data-formset]");
    const prefix = container.dataset.formset;
    const total = container.querySelector(`input[name="${prefix}-TOTAL_FORMS"]`);
    const template = container.querySelector("template[data-formset-empty]");
    const rows = container.querySelector("[data-formset-rows]");
    if (!total || !template || !rows) return;
    const index = Number(total.value);
    const fragment = document.createRange().createContextualFragment(
      template.innerHTML.replaceAll("__prefix__", String(index)));
    rows.append(fragment);
    total.value = String(index + 1);
    rows.lastElementChild?.querySelector("input, select, textarea")?.focus();
  });

  // × marks a form of the formset for deletion (its DELETE box, hidden), the rows keep their index.
  // A new row disappears; a saved one folds up until the form is saved, with "Undo".
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-formset-remove], [data-formset-undo]");
    if (!button) return;
    const row = button.closest("fieldset");
    const remove = button.matches("[data-formset-remove]");
    row.querySelector("[data-formset-delete]").checked = remove;
    if (!row.hasAttribute("data-saved")) {
      row.hidden = true;
      row.closest("[data-formset]")?.querySelector("[data-formset-add]")?.focus();
      return;
    }
    row.querySelector("[data-formset-body]").hidden = remove;
    row.querySelector("[data-formset-removed]").hidden = !remove;
    const removeButton = row.querySelector("[data-formset-remove]");
    removeButton.hidden = remove;
    (remove ? row.querySelector("[data-formset-undo]") : removeButton).focus();
  });

  SantaConsole.onInit((root) => {
    updateRulePreviews(root);
    updateBinaryPreviews(root);
    updateConditional(root);
    updateInherited(root);
    updateNewRuleFields(root);
    if (root.id === "drawer") uncheckSaved(root);
  });
  SantaConsole.onSwap((target) => {
    updateConditional(target.closest("form") || target);
    updateInherited(target);
    updateRulePreviews(target);
  });

  document.addEventListener("santa:selection", (event) => syncCreateRules(event.target));

  document.addEventListener("change", (event) => {
    const form = event.target.closest("form");
    if (form) {
      updateConditional(form);
      updateInherited(form);
      updateNewRuleFields(form);
    }
    if (event.target.matches("select[data-identifiers]")) updateRulePreviews(event.target.parentElement);
    if (form && event.target.matches('select[name$="rule_type"]')) updateBinaryPreviews(form);
  });

  document.addEventListener("click", (event) => {
    const confirmButton = event.target.closest("[data-confirm-when]");
    if (!confirmButton) return;
    const [name, value] = confirmButton.dataset.confirmWhen.split("=");
    if (controlValue(confirmButton.form, name) === value && !window.confirm(confirmButton.dataset.confirm)) {
      event.preventDefault();
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });
})();
