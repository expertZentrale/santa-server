// Allow as package (events/_packages_form.html): after saving, each package is checked by a request of its own, one
// after the other, then the binaries of the events (views_event_packages). A long check, a worker restarted or a
// proxy giving up stops only its own package: it shows that, the next one goes on, and it can be tried again.
(function () {
  "use strict";

  function next(steps) {
    if (steps.querySelector("[data-package-step][data-running]")) return;
    const step = steps.querySelector("[data-package-step][data-pending]");
    if (!step) return;
    delete step.dataset.pending;
    step.dataset.running = "1";
    const status = step.querySelector("[data-step-status]");
    if (status) status.textContent = steps.dataset.checking || status.textContent;
    // set up by htmx now: a list just swapped in may not be yet, the event would go nowhere
    htmx.process(step);
    htmx.trigger(step, "package-step");
  }

  function failed(step) {
    delete step.dataset.running;
    step.classList.remove("pending");
    step.classList.add("bad");
    const status = step.querySelector("[data-step-status]");
    if (status) {
      status.classList.add("bad");
      status.textContent = `✗ ${status.dataset.failedText || ""}`.trim();
    }
    step.querySelector("[data-step-failed]")?.removeAttribute("hidden");
    step.querySelector("[data-step-retry]")?.removeAttribute("hidden");
  }

  // the request of a step: ended (its answer replaced it) or failed (no answer, an error of the server)
  const steps = new WeakMap();
  document.addEventListener("htmx:beforeRequest", (event) => {
    const step = event.detail.elt?.closest?.("[data-package-step]");
    const list = step?.closest("[data-package-steps]");
    if (list && event.detail.xhr) steps.set(event.detail.xhr, { step, list });
  });

  function ended(event) {
    const entry = event.detail?.xhr && steps.get(event.detail.xhr);
    if (!entry) return;
    steps.delete(event.detail.xhr);
    const ok = event.type === "htmx:afterRequest" && event.detail.successful;
    if (!ok) failed(entry.step);
    else delete entry.step.dataset.running;
    // after the swap of the answer
    requestAnimationFrame(() => next(entry.list));
  }

  ["htmx:afterRequest", "htmx:sendError", "htmx:sendAbort", "htmx:timeout"]
    .forEach((name) => document.addEventListener(name, ended));

  document.addEventListener("click", (event) => {
    const retry = event.target.closest?.("[data-step-retry]");
    const step = retry?.closest("[data-package-step]");
    const list = step?.closest("[data-package-steps]");
    if (!list) return;
    retry.hidden = true;
    step.querySelector("[data-step-failed]")?.setAttribute("hidden", "");
    step.classList.remove("bad");
    step.classList.add("pending");
    step.querySelector("[data-step-status]")?.classList.remove("bad");
    step.dataset.pending = "1";
    // before the result of the binaries, again
    const verify = list.querySelector(".package-verify[data-package-step]");
    if (verify && !verify.dataset.running) verify.dataset.pending = "1";
    // after the one running now, if any
    next(list);
  });

  function setUp(root) {
    root.querySelectorAll?.("[data-package-steps]").forEach((list) => {
      if (list.dataset.started) return;
      list.dataset.started = "1";
      next(list);
    });
  }

  SantaConsole.onInit(setUp);
  SantaConsole.onSwap(setUp);
})();
