// Long actions that go on while the console does something else: the check of a package rule (seen by everyone who
// opens it, stopped by any of them, santa/progress.py run_source_check) and the preview of a package rule form (shown
// again when the form is opened again). Their views ask for the state while they are shown.
(function () {
  "use strict";

  const POLL = 1500;
  // a view that was left (kept for Back, or gone) stops asking after this long
  const GIVE_UP = 30 * 60 * 1000;

  // fn(): false to stop. Only while the element is in the page; asked again when it comes back (Back). fn checks
  // element.isConnected again after its own awaits: the view can be left while the answer travels.
  function poll(element, fn) {
    if (element.dataset.polling) return;
    element.dataset.polling = "1";
    let away = 0;
    const tick = async () => {
      if (!element.isConnected) {
        away += POLL;
        if (away < GIVE_UP) setTimeout(tick, POLL);
        else delete element.dataset.polling;
        return;
      }
      away = 0;
      let go = true;
      if (!document.hidden) {
        try {
          go = await fn();
        } catch {
          // the next try; a lost connection shows in its banner (core.js)
        }
      }
      if (go === false) delete element.dataset.polling;
      else setTimeout(tick, POLL);
    };
    setTimeout(tick, POLL);
  }

  async function getJson(url) {
    const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" },
                                        signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  // the view of the package rule again: in the drawer (from its URL, also when it came from a POST), or the page
  function reloadView(element) {
    // left meanwhile (another view, the drawer closed): what is shown now stays
    if (!element.isConnected) return;
    if (element.closest("#drawer") && element.dataset.viewUrl && window.htmx) {
      htmx.ajax("GET", element.dataset.viewUrl, { target: "#drawer", headers: { "X-Drawer-Replace": "1" } });
    } else if (element.closest("#drawer")) {
      SantaConsole.drawer?.reload();
    } else if (window.htmx) {
      const scroll = window.scrollY;
      htmx.ajax("GET", window.location.href, {
        target: document.body, swap: "innerHTML show:none", headers: { "HX-Boosted": "true", "X-Santa-Rerender": "1" },
      }).then(() => window.scrollTo(0, scroll));
    }
  }

  // --- The check of a package rule (sources/_check_status.html) ---

  function checkParts(status) {
    const view = status.closest(".drawer-panel, main") || document;
    // "Check now" and "Check the new packages"
    return {
      starts: [...view.querySelectorAll("form[data-check-start]")],
      cancel: view.querySelector("form[data-check-cancel]"),
    };
  }

  function showRunning(status, running) {
    status.hidden = !running;
    const { starts, cancel } = checkParts(status);
    starts.forEach((start) => { start.hidden = running; });
    if (cancel) cancel.hidden = !running;
  }

  function showCheck(status, data) {
    status.querySelector("[data-check-by]").textContent = data.by || "";
    status.querySelector("[data-check-step]").textContent = data.step || "";
    const cancelling = status.querySelector("[data-check-cancelling]");
    cancelling.textContent = data.cancelling || "";
    cancelling.hidden = !data.cancelling;
    setPercent(status, data.percent);
  }

  function setPercent(status, percent) {
    const bar = status.querySelector(".check-bar");
    bar.classList.toggle("determinate", Boolean(percent));
    bar.style.setProperty("--progress", `${Math.max(2, Number(percent) || 0)}%`);
  }

  function watchCheck(status) {
    poll(status, async () => {
      const data = await getJson(status.dataset.sourceCheck);
      if (!status.isConnected) return true;
      if (data.running) {
        showRunning(status, true);
        showCheck(status, data);
        return true;
      }
      // this page started it: its answer brings the view with the messages
      if (status.dataset.ownRequest) return true;
      reloadView(status);
      return false;
    });
  }

  function setUpChecks(root) {
    root.querySelectorAll?.("[data-source-check]").forEach((status) => {
      if (status.hidden) return;
      setPercent(status, status.dataset.percent);
      watchCheck(status);
    });
  }
  SantaConsole.onInit(setUpChecks);
  SantaConsole.onSwap(setUpChecks);

  // "Check now": at once "Stop the check" and the status, while the request runs; the others see it from the server
  document.addEventListener("htmx:beforeSend", (event) => {
    const form = event.detail.elt;
    if (!form?.matches?.("form[data-check-start]")) return;
    const status = (form.closest(".drawer-panel, main") || document).querySelector("[data-source-check]");
    if (!status) return;
    status.dataset.ownRequest = "1";
    status.querySelector("[data-check-by]").textContent = "";
    status.querySelector("[data-check-step]").textContent = status.dataset.starting || "";
    setPercent(status, 0);
    showRunning(status, true);
    event.detail.xhr.addEventListener("loadend", () => {
      delete status.dataset.ownRequest;
      // aborted or failed: the server tells how it is
      if (status.isConnected && event.detail.xhr.status !== 200) reloadView(status);
    }, { once: true });
    watchCheck(status);
  });

  // --- The preview of the package rule form (sources/_form.html) ---

  function previewParts(element) {
    const form = element.closest("form") || document;
    return { button: form.querySelector("[data-preview-button]"), cancel: form.querySelector("[data-preview-cancel]") };
  }

  function previewRunning(element, progressId) {
    const { button, cancel } = previewParts(element);
    if (button) button.hidden = Boolean(progressId);
    if (!cancel) return;
    cancel.hidden = !progressId;
    if (progressId) cancel.dataset.progressId = progressId;
    else delete cancel.dataset.progressId;
  }

  document.addEventListener("htmx:beforeSend", (event) => {
    const button = event.detail.elt;
    if (!button?.matches?.("[data-preview-button]")) return;
    const config = event.detail.requestConfig || {};
    const id = config.parameters?.progress_id || config.formData?.get?.("progress_id");
    if (!id) return;
    previewRunning(button, id);
    event.detail.xhr.addEventListener("loadend", () => previewRunning(button, null), { once: true });
  });

  document.addEventListener("click", (event) => {
    const cancel = event.target.closest("[data-preview-cancel]");
    if (!cancel?.dataset.progressId || !window.htmx) return;
    const url = cancel.dataset.cancelUrl.replace("00000000-0000-0000-0000-000000000000", cancel.dataset.progressId);
    cancel.disabled = true;
    htmx.ajax("POST", url, { source: cancel, swap: "none" }).finally(() => { cancel.disabled = false; });
  });

  // a form opened again while its preview runs: the result once it is there
  // no entry at all this long: the request is gone (its entry expired), the box stops waiting
  const PREVIEW_MISSING_AFTER = 15000;

  function setUpPreviews(root) {
    root.querySelectorAll?.("[data-preview-pending]").forEach((box) => {
      const since = Date.now();
      poll(box, async () => {
        const data = await getJson(box.dataset.previewPending);
        if (!box.isConnected) return true;
        const missing = !("done" in data) && Date.now() - since > PREVIEW_MISSING_AFTER;
        const step = box.querySelector("[data-preview-step]");
        if (step && (data.current || data.label)) {
          step.textContent = [data.current && data.total ? `${data.current} / ${data.total}` : "", data.label || ""]
            .filter(Boolean).join(" · ");
        }
        if (!data.done && !(data.beat_age > 35) && !missing) return true;
        delete box.dataset.previewPending;
        previewRunning(box, null);
        htmx.ajax("GET", box.dataset.resultUrl, { source: box, target: box, swap: "innerHTML" });
        return false;
      });
    });
  }
  SantaConsole.onInit(setUpPreviews);
  SantaConsole.onSwap(setUpPreviews);
})();
