// The drawer (#drawer): details and forms. Every view of it is an entry of the browser history (same page URL,
// the view in history.state), so Back and Forward of the browser and of the mouse step through the views. The views
// left are kept (their DOM, with what was entered) and come back as they were; a view not kept any more (after a
// reload) comes again from the server.
(function () {
  "use strict";

  // history.state of a view: {drawer: url ("" for a view of a POST, it can't be loaded again), index, chain}.
  // index counts the entries since the page (0, no drawer); chain is the index of the view opened from the page,
  // the views after it in the chain were opened from inside the drawer ("Back" in the drawer goes to them).
  const kept = new Map();
  let shown = 0;
  let chain = 0;
  let drawerSaved = false;
  let reloadAfterBack = false;
  // a press that started in the drawer: selecting text and letting go next to it doesn't close it
  let pressedInside = false;

  function getDrawer() {
    return document.getElementById("drawer");
  }

  function isOpen() {
    return !!getDrawer()?.classList.contains("open");
  }

  function currentState() {
    return history.state && typeof history.state.index === "number" ? history.state : null;
  }

  function keepView(drawer) {
    if (!drawer.firstChild) return;
    const view = document.createDocumentFragment();
    const scroll = drawer.scrollTop;
    while (drawer.firstChild) view.append(drawer.firstChild);
    kept.set(shown, { view, scroll });
  }

  function hideDrawer(drawer) {
    // the drawer saved something the page behind it doesn't show yet
    if (drawerSaved || drawer.querySelector("[data-refresh-on-close]")) {
      window.location.reload();
      return;
    }
    keepView(drawer);
    drawer.classList.remove("open");
  }

  function updateDrawerBack(drawer) {
    drawer.querySelectorAll("[data-drawer-back]").forEach((button) => button.remove());
    const canGoBack = shown > chain;
    // the links back of the templates are for the page, the history knows the way back
    drawer.querySelectorAll(".drawer-head .back").forEach((link) => {
      link.hidden = canGoBack;
    });
    const tools = drawer.querySelector(".drawer-tools");
    const template = document.getElementById("drawer-back");
    if (canGoBack && tools && template) tools.prepend(template.content.cloneNode(true));
  }

  // × and Escape: back to the entry of the page, the drawer closes there (popstate)
  function closeDrawer() {
    if (shown > 0 && currentState()) {
      history.go(-shown);
    } else {
      hideDrawer(getDrawer());
    }
  }

  // a view of the drawer again from the server, after something was saved in the view after it
  function reloadDrawerView(drawer) {
    const createRules = drawer.querySelector("[data-create-rules]");
    if (createRules) {
      htmx.ajax("POST", createRules.getAttribute("action"), {
        source: createRules, target: "#drawer", values: { refresh: "1" }, headers: { "X-Drawer-Reload": "1" },
      });
      return;
    }
    const url = drawer.querySelector("[data-drawer-url]")?.dataset.drawerUrl;
    if (url) htmx.ajax("GET", url, { target: "#drawer", headers: { "X-Drawer-Reload": "1" } });
  }

  function showState(state) {
    const drawer = getDrawer();
    if (!state || !("drawer" in state)) {
      shown = state ? state.index : 0;
      if (isOpen()) hideDrawer(drawer);
      return;
    }
    if (isOpen()) keepView(drawer);
    shown = state.index;
    chain = state.chain;
    const view = kept.get(shown);
    if (view) {
      kept.delete(shown);
      drawer.replaceChildren(view.view);
      drawer.classList.add("open");
      drawer.scrollTop = view.scroll;
      updateDrawerBack(drawer);
      drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
      if (reloadAfterBack) {
        reloadAfterBack = false;
        reloadDrawerView(drawer);
      }
    } else if (state.drawer) {
      // from the server it is up to date anyway
      reloadAfterBack = false;
      htmx.ajax("GET", state.drawer, { target: "#drawer", headers: { "X-Drawer-Restore": "1" } });
    } else {
      // the view of a POST is gone (reload): nothing to show
      drawer.replaceChildren();
      drawer.classList.remove("open");
    }
  }

  window.addEventListener("popstate", (event) => {
    if (event.state?.htmx) return;
    showState(currentState());
  });

  // what the next swap into the drawer does to the history: a new entry, or none
  let pending = null;

  document.addEventListener("htmx:beforeSwap", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer || !event.detail.shouldSwap) return;
    const config = event.detail.requestConfig || {};
    const headers = config.headers || {};
    if (headers["X-Drawer-Reload"] || headers["X-Drawer-Restore"]) {
      pending = null;
      return;
    }
    if (headers["X-Drawer-Replace"]) {
      pending = "replace";
      return;
    }
    const inside = config.elt && config.elt !== drawer && drawer.contains(config.elt);
    if (inside && config.verb !== "get") {
      // a form of the view answers in the same view (errors, a refresh): the same entry
      pending = null;
      return;
    }
    pending = inside && isOpen() ? "push" : "start";
    if (isOpen()) keepView(drawer);
  });

  // after core.js opened the drawer and set up the new view
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer) return;
    const url = drawer.querySelector("[data-drawer-url]")?.dataset.drawerUrl || "";
    if (pending === "push" || pending === "start") {
      const index = (currentState()?.index ?? shown) + 1;
      // a new entry drops the entries after the current one, and so their views
      [...kept.keys()].forEach((key) => { if (key >= index) kept.delete(key); });
      shown = index;
      chain = pending === "start" ? index : chain;
      history.pushState({ drawer: url, index, chain }, "");
    } else if (pending === "replace") {
      history.replaceState({ drawer: url, index: shown, chain }, "");
    }
    pending = null;
    updateDrawerBack(drawer);
    drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
  });

  // drawer_done(): back to the view before and update it, or reload the page when there is none. With
  // {"drawerSaved": {"open": url}} the saved object opens in the drawer instead (e.g. a new group and its profile).
  document.addEventListener("drawerSaved", (event) => {
    const drawer = getDrawer();
    drawerSaved = true;
    const open = event.detail?.open;
    if (open) {
      htmx.ajax("GET", open, { target: "#drawer", headers: { "X-Drawer-Replace": "1" } });
    } else if (shown > chain && currentState()) {
      reloadAfterBack = true;
      history.back();
    } else {
      // the page again, without this view: a Back after the reload doesn't open the saved form again
      if (currentState()) history.replaceState({ index: shown }, "");
      drawer.classList.remove("open");
      window.location.reload();
    }
  });

  document.addEventListener("pointerdown", (event) => {
    pressedInside = !!event.target.closest?.("#drawer");
  }, true);

  document.addEventListener("click", (event) => {
    const wasInside = pressedInside;
    pressedInside = false;
    // × closes the drawer; "Cancel" of a view opened from another one goes back to it
    const close = event.target.closest("[data-drawer-close]");
    if (close && shown > chain && !close.closest(".drawer-tools")) history.back();
    else if (close) closeDrawer();
    if (event.target.closest("[data-drawer-back]")) history.back();
    // a click next to the open drawer closes it, except on what opens another one
    const drawer = getDrawer();
    // "create rules" follows the selection: selecting rows of the list keeps it open
    const selecting = drawer?.querySelector("[data-create-rules]")
                      && event.target.closest("[data-select-form] tbody, [data-select-all]");
    if (isOpen() && event.target.isConnected && !event.target.closest("#drawer")
        && !event.target.closest('[hx-target="#drawer"]') && !selecting
        && !wasInside) {
      closeDrawer();
    }
  });

  // Escape closes the drawer, and only the drawer: table.js would clear the selection behind it
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isOpen()) return;
    event.stopImmediatePropagation();
    closeDrawer();
  });

  // a view of the drawer in the history (a reload, or Back from another page): it opens again
  document.addEventListener("DOMContentLoaded", () => {
    const state = currentState();
    // a link to this page starts without a drawer (Chrome can keep the state of the URL)
    const navigation = performance.getEntriesByType?.("navigation")[0]?.type;
    if (state && navigation === "navigate") {
      history.replaceState(null, "");
    } else if (state?.drawer) {
      showState(state);
    } else if (state) {
      shown = state.index;
    }
  });

  SantaConsole.drawer = { isOpen, close: closeDrawer };
})();
