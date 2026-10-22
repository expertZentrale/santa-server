// Forms: fields shown depending on other fields, previews of the rule identifiers, confirmations, and the drawer
// "create rules" that follows the selection of the list behind it
(function () {
  "use strict";

  const { controlValue, fitTextarea } = SantaConsole;

  function fitTextareas(root) {
    root.querySelectorAll("textarea").forEach(fitTextarea);
  }

  // data-show-when="policy=BLOCKLIST|CEL", data-show-when="parent" (not empty), data-hide-when="is_global"
  function updateConditional(root) {
    root.querySelectorAll("[data-show-when], [data-hide-when]").forEach((element) => {
      // a row of a formset decides by its own fields (downloads-3-url_from), the others by the whole form
      const form = element.closest("[data-formset-row]") || element.closest("form") || document;
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
    // a field that was just shown had no height to fit while it was hidden; after the other updates of this event
    // (updateInherited, updateNewRuleFields), which may show more
    queueMicrotask(() => fitTextareas(root));
    // data-disable-when="keep_versions=0|": the option makes no sense then, it is switched off and greyed
    root.querySelectorAll("[data-disable-when]").forEach((element) => {
      const form = element.closest("form") || document;
      const [name, values] = element.dataset.disableWhen.split("=");
      const disabled = values.split("|").includes(controlValue(form, name).trim());
      element.classList.toggle("is-disabled", disabled);
      element.querySelectorAll("input, select, textarea").forEach((control) => {
        control.disabled = disabled;
        if (disabled && control.type === "checkbox") control.checked = false;
      });
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

  // Events → package rules: a package rule of another catalog is not offered for a row; when the catalog changes,
  // a chosen one of the old catalog goes back to "new package rule"
  function updatePackageTargets(root) {
    const forms = root.matches?.("form[data-event-packages]") ? [root]
      : root.querySelectorAll("form[data-event-packages]");
    forms.forEach((form) => {
      let kinds = {};
      try {
        kinds = JSON.parse(form.dataset.sourceKinds || "{}");
      } catch (error) {
        return;
      }
      form.querySelectorAll('select[name^="kind_"]').forEach((kindSelect) => {
        const target = form.querySelector(`select[name="target_${kindSelect.name.slice(5)}"]`);
        if (!target) return;
        [...target.options].forEach((option) => {
          option.hidden = option.value !== "new" && kinds[option.value] !== kindSelect.value;
          option.disabled = option.hidden;
        });
        if (target.selectedOptions[0]?.hidden) target.value = "new";
      });
    });
  }

  // Events → package rules, the bar "for all checked packages": a catalog and a package rule copied into every
  // checked row. A package rule brings its catalog; the rows change as if chosen by hand (change events).
  function setupPackageBulk(root) {
    root.querySelectorAll("[data-package-bulk]").forEach((bar) => {
      if (bar.dataset.bulkReady) return;
      bar.dataset.bulkReady = "1";
      const form = bar.closest("form");
      const kindSelect = bar.querySelector("[data-bulk-kind]");
      const targetSelect = bar.querySelector("[data-bulk-target]");
      const status = bar.querySelector("[data-bulk-status]");
      let kinds = {};
      try {
        kinds = JSON.parse(form.dataset.sourceKinds || "{}");
      } catch (error) {
        return;
      }
      const filterTargets = () => {
        [...targetSelect.options].forEach((option) => {
          const other = kinds[option.value] && kindSelect.value && kinds[option.value] !== kindSelect.value;
          option.hidden = Boolean(other);
          option.disabled = Boolean(other);
        });
        if (targetSelect.selectedOptions[0]?.hidden) targetSelect.value = "";
      };
      kindSelect.addEventListener("change", filterTargets);
      targetSelect.addEventListener("change", () => {
        if (kinds[targetSelect.value]) kindSelect.value = kinds[targetSelect.value];
        filterTargets();
      });
      const set = (select, value) => {
        if (!select || select.value === value) return;
        select.value = value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      };
      bar.querySelector("[data-bulk-apply]").addEventListener("click", () => {
        const kind = kindSelect.value || kinds[targetSelect.value] || "";
        let count = 0;
        form.querySelectorAll('input[type="checkbox"][name^="approve_"]:checked').forEach((checkbox) => {
          const index = checkbox.name.slice("approve_".length);
          if (kind) set(form.querySelector(`select[name="kind_${index}"]`), kind);
          if (targetSelect.value) set(form.querySelector(`select[name="target_${index}"]`), targetSelect.value);
          count += 1;
        });
        status.textContent = bar.dataset.applied.replace("%(count)s", count);
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

  // The drawer "create rules" follows the selection of the list behind it: a click, Ctrl- or Shift-click adds or
  // removes binaries. A binary no longer selected leaves at once, without a request; the form stays as it is (what
  // was entered, the checks). New binaries show at once (the name of the row, "loading…") and come in one request
  // after a short pause (events_create_rules_binaries), only them.
  let syncTimer = 0;
  let syncController = null;
  // A save of the drawer form ("Create rules") is never changed under it: the sync waits for its answer
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.elt.matches?.("[data-create-rules]")) event.detail.elt.dataset.saving = "1";
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.elt.matches?.("[data-create-rules]")) delete event.detail.elt.dataset.saving;
  });

  // {sha: {name, ids}} of the checked rows: the ids of the events ("All events"), none for a blocked app (its sha
  // stands for all its open blocks)
  function selectedBinaries(listForm) {
    const binaries = new Map();
    SantaConsole.table.rowBoxes(listForm).filter((box) => box.checked).forEach((box) => {
      const sha = box.dataset.sha || box.value;
      if (!binaries.has(sha)) {
        binaries.set(sha, { name: box.closest("tr")?.querySelector("a.strong")?.textContent.trim() || sha.slice(0, 12),
                            ids: [], byEvent: box.name === "ids" });
      }
      if (box.name === "ids") binaries.get(sha).ids.push(box.value);
    });
    return binaries;
  }

  const sameIds = (item, ids) => [...ids].sort().join(" ") === (item.dataset.ids || "").split(" ").sort().join(" ");

  // the binaries of the drawer as they will be: the ones no longer selected removed, new ones (or ones with other
  // events) as placeholders. Returns the query of the binaries to load, "" for none.
  function showSelection(drawerForm, binaries) {
    const list = drawerForm.querySelector("[data-binaries]");
    const query = new URLSearchParams();
    list.querySelectorAll(":scope > li").forEach((item) => {
      const sha = item.dataset.sha || item.dataset.pendingSha;
      if (!binaries.has(sha)) item.remove();
    });
    binaries.forEach((binary, sha) => {
      const item = list.querySelector(`:scope > li[data-sha="${CSS.escape(sha)}"]`);
      const pending = list.querySelector(`:scope > li[data-pending-sha="${CSS.escape(sha)}"]`);
      if (item && (!binary.byEvent || sameIds(item, binary.ids))) {
        // back to the events it was loaded with
        delete item.dataset.stale;
        item.querySelector("[data-failed]")?.remove();
        return;
      }
      if (binary.byEvent) binary.ids.forEach((id) => query.append("ids", id));
      else query.append("shas", sha);
      if (item || pending) {
        (item || pending).dataset.stale = "1";
        return;
      }
      const placeholder = document.createElement("li");
      placeholder.className = "pending";
      placeholder.dataset.pendingSha = sha;
      const strong = document.createElement("strong");
      strong.textContent = binary.name;
      const note = document.createElement("small");
      note.className = "muted";
      note.textContent = list.dataset.pendingLabel || "…";
      placeholder.append(strong, " ", note);
      list.append(placeholder);
    });
    updateCreateRules(drawerForm);
    return query.toString();
  }

  // what depends on the binaries: the events posted (both forms of the drawer), the title, the rule types, the
  // Team ID of the CEL help, the count of "As package", and the hint when none is left
  function updateCreateRules(drawerForm) {
    const drawer = drawerForm.closest(".drawer-panel") || drawerForm.parentElement;
    const items = [...drawerForm.querySelectorAll("[data-binaries] > li")];
    const loaded = items.filter((item) => item.dataset.sha);
    // a binary whose selected events changed still has the old ones until its new version is loaded
    const ready = loaded.filter((item) => !item.dataset.stale);
    const tabs = drawer.querySelector(".allow-tabs");
    [drawerForm, tabs].filter(Boolean).forEach((form) => {
      form.querySelectorAll('input[type="hidden"][name="ids"]').forEach((input) => input.remove());
      ready.flatMap((item) => (item.dataset.ids || "").split(" ").filter(Boolean)).forEach((id) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "ids";
        input.value = id;
        form.prepend(input);
      });
    });
    drawerForm.hidden = !items.length;
    if (tabs) tabs.hidden = !items.length;
    const empty = drawer.querySelector("[data-no-binaries]");
    if (empty) empty.hidden = Boolean(items.length);
    // saved only with every binary loaded: a placeholder has no events yet, a stale one the old ones
    drawerForm.querySelectorAll('button[name="apply"]').forEach((button) => {
      button.disabled = ready.length < items.length;
    });
    try {
      const titles = JSON.parse(drawerForm.dataset.titles);
      const plural = items.length === 1 ? "one" : "other";
      const title = drawer.querySelector("[data-drawer-title]");
      if (title) title.textContent = titles[plural].replace("%(count)s", items.length);
      const button = drawerForm.querySelector('button[name="apply"]');
      if (button) button.textContent = titles[`button_${plural}`];
    } catch (error) {
      // no titles: nothing to change
    }
    const types = new Set(loaded.flatMap((item) => (item.dataset.types || "").split(" ")));
    const select = drawerForm.querySelector('select[name="rule_type"]');
    select?.querySelectorAll("option").forEach((option) => {
      const unused = Boolean(option.value) && Boolean(loaded.length) && !types.has(option.value);
      option.hidden = unused;
      option.disabled = unused && option.value !== select.value;
    });
    const teams = new Set(loaded.map((item) => item.dataset.team));
    const cel = drawerForm.querySelector('[data-show-when="policy=CEL"]');
    if (cel) {
      if (teams.size === 1 && [...teams][0]) cel.dataset.teamId = [...teams][0];
      else delete cel.dataset.teamId;
    }
    const count = drawer.querySelector(".allow-tabs .tab-count");
    const packages = new Set(loaded.map((item) => item.dataset.package).filter(Boolean)).size;
    if (count) {
      count.textContent = packages;
      count.hidden = !packages;
    }
  }

  // the binaries loaded: in place of their placeholders (or of their older version, with the checks and the prefix
  // entered there)
  function addBinaries(drawerForm, html) {
    const list = drawerForm.querySelector("[data-binaries]");
    const template = document.createElement("template");
    template.innerHTML = html;
    [...template.content.children].filter((item) => item.matches("li[data-sha]")).forEach((item) => {
      const sha = item.dataset.sha;
      const old = list.querySelector(`:scope > li[data-sha="${CSS.escape(sha)}"], :scope > li[data-pending-sha="${CSS.escape(sha)}"]`);
      if (old?.dataset.sha) {
        item.querySelectorAll("input").forEach((input) => {
          const before = old.querySelector(`input[name="${CSS.escape(input.name)}"]`);
          if (!before) return;
          if (input.type === "checkbox") input.checked = before.checked;
          else input.value = before.value;
        });
      }
      if (old) old.replaceWith(item);
    });
    // no longer in the answer (e.g. resolved meanwhile): a placeholder goes, an older version stays
    list.querySelectorAll(":scope > li.pending").forEach((item) => item.remove());
    list.querySelectorAll(":scope > li[data-stale]").forEach((item) => delete item.dataset.stale);
    updateCreateRules(drawerForm);
    updateBinaryPreviews(drawerForm);
  }

  function failBinaries(drawerForm) {
    const list = drawerForm.querySelector("[data-binaries]");
    list.querySelectorAll(":scope > li.pending small").forEach((note) => {
      note.textContent = list.dataset.failedLabel || "!";
      note.classList.add("error");
    });
    // a binary with other events: it keeps its old version, which can't be saved
    list.querySelectorAll(":scope > li[data-sha][data-stale]").forEach((item) => {
      if (item.querySelector("[data-failed]")) return;
      const note = document.createElement("small");
      note.className = "error";
      note.dataset.failed = "";
      note.textContent = list.dataset.failedLabel || "!";
      item.append(note);
    });
  }

  function syncCreateRules(listForm) {
    const drawer = document.getElementById("drawer");
    const drawerForm = drawer.querySelector("[data-create-rules]");
    if (!drawerForm || !drawer.classList.contains("open") || drawer.contains(listForm)) return;
    if (drawerForm.dataset.saving) {
      drawerForm.addEventListener("htmx:afterRequest", () => syncCreateRules(listForm), { once: true });
      return;
    }
    const query = showSelection(drawerForm, selectedBinaries(listForm));
    // the status filter of the list: which events of a blocked app
    const resolved = listForm.querySelector('[name="resolved"]');
    drawer.querySelectorAll('[name="resolved"]').forEach((input) => {
      if (resolved) input.value = resolved.value;
    });
    clearTimeout(syncTimer);
    syncController?.abort();
    syncController = null;
    if (!query) return;
    // a Shift-click or quick Ctrl-clicks: one request after them
    syncTimer = setTimeout(async () => {
      const controller = new AbortController();
      syncController = controller;
      try {
        const params = new URLSearchParams(query);
        if (resolved) params.set("resolved", resolved.value);
        const response = await fetch(`${drawerForm.dataset.binariesUrl}?${params}`, { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const html = await response.text();
        if (controller.signal.aborted || !drawerForm.isConnected) return;
        addBinaries(drawerForm, html);
      } catch (error) {
        // aborted by a newer selection: it loads its own
        if (!controller.signal.aborted && drawerForm.isConnected) failBinaries(drawerForm);
      } finally {
        if (syncController === controller) syncController = null;
      }
    }, 300);
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
    // its fields as chosen in it (a new row has no change event yet)
    if (rows.lastElementChild) updateConditional(rows.lastElementChild);
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
    updatePackageTargets(root);
    setupPackageBulk(root);
    updateNewRuleFields(root);
    root.querySelectorAll("[data-create-rules]").forEach(updateCreateRules);
    if (root.id === "drawer") uncheckSaved(root);
  });
  SantaConsole.onSwap((target) => {
    updateConditional(target.closest("form") || target);
    updateInherited(target);
    updateRulePreviews(target);
  });

  document.addEventListener("santa:selection", (event) => syncCreateRules(event.target));

  document.addEventListener("input", (event) => {
    if (event.target.tagName === "TEXTAREA") fitTextarea(event.target);
  });
  // the handle of a textarea was dragged: that height stays (fitTextarea)
  document.addEventListener("pointerup", (event) => {
    const textarea = event.target;
    if (textarea.tagName !== "TEXTAREA" || !textarea.dataset.fitHeight) return;
    const height = textarea.getBoundingClientRect().height;
    if (Math.abs(height - parseFloat(textarea.dataset.fitHeight)) > 1) {
      textarea.dataset.userHeight = height;
      textarea.dataset.fitHeight = height;
    }
  });
  // other widths wrap the lines differently
  window.addEventListener("resize", SantaConsole.debounce(() => fitTextareas(document), 150));

  // a number typed changes the options that depend on it before leaving the field
  document.addEventListener("input", (event) => {
    const form = event.target.closest("form");
    if (form && event.target.type === "number") updateConditional(form);
  });

  document.addEventListener("change", (event) => {
    const form = event.target.closest("form");
    if (form) {
      updateConditional(form);
      updateInherited(form);
      updatePackageTargets(form);
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

  // capture: before htmx sends a boosted form; cancelled, the form is not sent at all
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
})();
