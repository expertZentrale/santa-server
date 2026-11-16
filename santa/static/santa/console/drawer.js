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
  // a step back through the history that hasn't arrived yet (popstate): the two clicks of a double-click come before
  // it, a second step would go one entry too far (the page before, or another state of the list)
  let goingBack = false;

  function goBack(steps) {
    if (goingBack) return;
    goingBack = true;
    history.go(-steps);
  }

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
    // the placeholder of a view that loads is no view to come back to
    if (!drawer.firstChild || isLoading(drawer)) return;
    const view = document.createDocumentFragment();
    const scroll = drawer.scrollTop;
    while (drawer.firstChild) view.append(drawer.firstChild);
    kept.set(shown, { view, scroll });
  }

  // The page behind the drawer again, in place (as a boosted link, core.js): it shows what was saved and the
  // messages; the scroll position stays
  function refreshPage() {
    drawerSaved = false;
    const scroll = window.scrollY;
    return htmx.ajax("GET", window.location.href, {
      target: document.body, swap: "innerHTML show:none", headers: { "HX-Boosted": "true", "X-Santa-Rerender": "1" },
    }).then(() => window.scrollTo(0, scroll));
  }

  // the view asked for when the drawer was closed: it shows at once, with a placeholder until the view comes
  let loadingRequest = null;

  function isLoading(drawer) {
    return !!drawer.querySelector(":scope > [data-drawer-loading]");
  }

  // Another view in the open drawer: its content comes in from the side it goes to (Back: from the left), the drawer
  // itself stays; only replacing the content was easy to miss (drawer.css)
  function showViewChange(drawer, direction) {
    const panel = drawer.querySelector(":scope > .drawer-panel");
    if (!panel) return;
    const name = direction === "back" ? "drawer-view-back" : "drawer-view-in";
    panel.classList.remove("drawer-view-in", "drawer-view-back");
    panel.classList.add(name);
    panel.addEventListener("animationend", () => panel.classList.remove(name), { once: true });
  }
  // how the next view swapped in shows: "in", "back", or nothing (the first one, a refresh, a form's answer)
  let viewChange = null;
  // the direction of a view of the history that comes from the server again (Back / Forward to a view not kept)
  let restoreChange = null;

  function hideDrawer(drawer) {
    if (isLoading(drawer)) {
      // closed before its view came: it doesn't open again for it
      if (loadingRequest) SantaConsole.abortRequest(loadingRequest);
      loadingRequest = null;
      drawer.replaceChildren();
      drawer.classList.remove("open", "loading");
      return;
    }
    // the drawer saved something the page behind it doesn't show yet
    if (drawerSaved || drawer.querySelector("[data-refresh-on-close]")) {
      drawer.classList.remove("open");
      refreshPage();
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
      goBack(shown);
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
      // kept under its own index, so that Forward brings it back with what was entered
      if (isOpen()) hideDrawer(drawer);
      shown = state ? state.index : 0;
      return;
    }
    const wasOpen = isOpen() && !isLoading(drawer);
    const direction = state.index < shown ? "back" : "in";
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
      if (wasOpen) showViewChange(drawer, direction);
      drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
      if (reloadAfterBack) {
        reloadAfterBack = false;
        reloadDrawerView(drawer);
      }
    } else if (state.drawer) {
      // from the server it is up to date anyway
      reloadAfterBack = false;
      restoreChange = wasOpen ? direction : null;
      htmx.ajax("GET", state.drawer, { target: "#drawer", headers: { "X-Drawer-Restore": "1" } });
    } else {
      // the view of a POST is gone (reload): nothing to show
      drawer.replaceChildren();
      drawer.classList.remove("open");
    }
  }

  // Another page loaded without a reload (hx-boost, core.js): the views kept were views of the page before. Its
  // history entry becomes the entry of a page without a view (like after a load): htmx would load the page again
  // when the drawer closes back to it. An entry of another URL (Back to the page before) loads that page.
  const here = () => window.location.pathname + window.location.search;
  let pageUrl = here();
  document.addEventListener("santa:page", () => {
    goingBack = false;
    kept.clear();
    shown = 0;
    chain = 0;
  });
  window.addEventListener("pagehide", () => {
    history.scrollRestoration = "auto";
  });
  document.addEventListener("htmx:pushedIntoHistory", () => {
    pageUrl = here();
    history.scrollRestoration = "auto";
    history.replaceState({ index: 0, chain: 0 }, "");
  });
  // Before it pushes the next page, htmx marks the entry it leaves as its own ({htmx: true}): the view of the drawer
  // of that entry ("Open as page") would be lost, and Back to it would let htmx load the page again on every close
  let leaving = null;
  document.addEventListener("htmx:beforeHistorySave", () => {
    leaving = currentState();
    history.scrollRestoration = "auto";
  });
  document.addEventListener("htmx:beforeHistoryUpdate", () => {
    if (leaving) history.replaceState(leaving, "");
    leaving = null;
  });

  window.addEventListener("popstate", (event) => {
    goingBack = false;
    // an entry of htmx: it restores that page itself
    if (event.state?.htmx) {
      pageUrl = here();
      return;
    }
    if (here() !== pageUrl) {
      // the entry of another page (loaded in place before): that page again, with the view of its drawer
      pageUrl = here();
      const url = pageUrl;
      const state = currentState();
      refreshPage().then(() => {
        // unless Back / Forward went on meanwhile
        if (state?.drawer && here() === url && currentState()?.index === state.index) showState(state);
      });
      return;
    }
    showState(currentState());
  });

  // what the next swap into the drawer does to the history: a new entry, or none
  let pending = null;

  // A view opens at once: a click shows the drawer right away (or, open, that it loads), not only when the server
  // answered; also a POST from the page ("Create rules…" of a selection). A form of the view saving (a POST from
  // inside) and a reload of the view shown keep their view as it is.
  document.addEventListener("htmx:beforeRequest", (event) => {
    const drawer = getDrawer();
    const config = event.detail.requestConfig || {};
    if (event.detail.target !== drawer || (config.headers || {})["X-Drawer-Reload"]) return;
    const inside = config.elt && config.elt !== drawer && drawer.contains(config.elt);
    if (inside && (config.verb || "get") !== "get") return;
    drawer.classList.add("loading");
    const template = document.getElementById("drawer-loading");
    if (isOpen() || !template) return;
    drawer.replaceChildren(template.content.cloneNode(true));
    drawer.classList.add("open");
    loadingRequest = event.detail.xhr;
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer) return;
    drawer.classList.remove("loading");
    if (event.detail.xhr === loadingRequest) loadingRequest = null;
    // no view came (an error, a stopped request): the placeholder goes again
    if (isLoading(drawer)) {
      drawer.replaceChildren();
      drawer.classList.remove("open");
    }
  });

  // a request sent from a view of the drawer: it answers into that view only while it is shown
  document.addEventListener("htmx:beforeRequest", (event) => {
    const config = event.detail.requestConfig;
    const elt = event.detail.elt;
    if (config && elt && elt !== getDrawer() && getDrawer()?.contains(elt)) config.fromDrawer = true;
  });

  // The view that sent it was left meanwhile (the drawer closed, another view opened: a check of a package rule goes
  // on): its answer doesn't open the drawer again over what is done now. Its messages show as toasts; the view kept
  // for Back is dropped, it comes again from the server.
  document.addEventListener("htmx:beforeSwap", (event) => {
    const drawer = getDrawer();
    const config = event.detail.requestConfig || {};
    if (event.detail.target !== drawer || !config.fromDrawer || !event.detail.shouldSwap) return;
    const elt = config.elt;
    if (elt && isOpen() && drawer.contains(elt)) return;
    event.detail.shouldSwap = false;
    SantaConsole.showToastsOf?.(event.detail.serverResponse || "");
    [...kept].forEach(([index, entry]) => {
      if (elt && entry.view.contains(elt)) kept.delete(index);
    });
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer || !event.detail.shouldSwap) return;
    const config = event.detail.requestConfig || {};
    const headers = config.headers || {};
    const wasOpen = isOpen() && !isLoading(drawer);
    const restore = restoreChange;
    restoreChange = null;
    viewChange = null;
    if (headers["X-Drawer-Reload"] || headers["X-Drawer-Restore"]) {
      // the same view fresh from the server: no change to show; one of the history: from its side
      viewChange = headers["X-Drawer-Restore"] && wasOpen ? restore : null;
      pending = null;
      return;
    }
    if (headers["X-Drawer-Replace"]) {
      // drawerSaved with another view to open (not the same one again, e.g. an event marked resolved)
      const url = drawer.querySelector("[data-drawer-url]")?.dataset.drawerUrl;
      viewChange = wasOpen && config.path && config.path !== url ? "in" : null;
      pending = "replace";
      return;
    }
    const inside = config.elt && config.elt !== drawer && drawer.contains(config.elt);
    if (inside && config.verb !== "get") {
      // a form of the view answers in the same view (errors, a refresh): the same entry
      pending = null;
      return;
    }
    viewChange = wasOpen ? "in" : null;
    pending = inside && isOpen() ? "push" : "start";
    if (isOpen()) keepView(drawer);
  });

  // after core.js opened the drawer and set up the new view
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer) return;
    const url = drawer.querySelector("[data-drawer-url]")?.dataset.drawerUrl || "";
    const current = currentState();
    if ((pending === "push" || pending === "start") && url && shown > 0 && current?.index === shown
        && current.drawer === url) {
      // the view it shows already (the same row clicked again): fresh content, but no second entry to step through
      kept.delete(shown);
      pending = "replace";
    }
    if (pending === "push" || pending === "start") {
      const index = (currentState()?.index ?? shown) + 1;
      // a new entry drops the entries after the current one, and so their views
      [...kept.keys()].forEach((key) => { if (key >= index) kept.delete(key); });
      shown = index;
      chain = pending === "start" ? index : chain;
      // Back and Forward between the page and the views of its drawer leave the page where it is: the browser would
      // scroll it back to where it was when the view opened. Set on the entry of the page, the new one inherits it;
      // not "auto" again on the way back, Forward and Back again would scroll it. Only once the page is left (a
      // reload, another page) its entry restores the position again.
      history.scrollRestoration = "manual";
      history.pushState({ drawer: url, index, chain }, "");
    } else if (pending === "replace") {
      history.replaceState({ drawer: url, index: shown, chain }, "");
    }
    pending = null;
    updateDrawerBack(drawer);
    drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
    if (viewChange) showViewChange(drawer, viewChange);
    viewChange = null;
  });

  // drawer_done(): back to the view before and update it, or the page again when there is none. With
  // {"drawerSaved": {"open": url}} the saved object opens in the drawer instead (e.g. a new group and its profile).
  document.addEventListener("drawerSaved", (event) => {
    const drawer = getDrawer();
    drawerSaved = true;
    const open = event.detail?.open;
    if (open) {
      // the same view again (an event marked resolved): where it was; another one from its top
      const same = drawer?.querySelector("[data-drawer-url]")?.dataset.drawerUrl === open;
      const scroll = same ? drawer.scrollTop : 0;
      htmx.ajax("GET", open, { target: "#drawer", headers: { "X-Drawer-Replace": "1" } })
        .then(() => { getDrawer().scrollTop = scroll; });
    } else if (shown > chain && currentState()) {
      reloadAfterBack = true;
      goBack(1);
    } else {
      // the page again, without this view: a Back after the reload doesn't open the saved form again
      if (currentState()) history.replaceState({ index: shown }, "");
      drawer.classList.remove("open");
      refreshPage();
    }
  });

  document.addEventListener("pointerdown", (event) => {
    pressedInside = !!event.target.closest?.("#drawer");
  }, true);

  // the time a double-click may take (the second click comes within it on most systems)
  const DOUBLE_CLICK_MS = 300;
  let closeTimer = null;
  function cancelClose() {
    clearTimeout(closeTimer);
    closeTimer = null;
  }

  document.addEventListener("click", (event) => {
    const wasInside = pressedInside;
    pressedInside = false;
    // × closes the drawer; "Cancel" of a view opened from another one goes back to it
    const close = event.target.closest("[data-drawer-close]");
    if (close && shown > chain && !close.closest(".drawer-tools")) goBack(1);
    else if (close) closeDrawer();
    if (event.target.closest("[data-drawer-back]")) goBack(1);
    // a click next to the open drawer closes it, except on what opens another one, and the top bar (its menus, a press
    // that missed a link), the toasts and the banners (data-keeps-drawer); a link of the top bar loads another page
    const drawer = getDrawer();
    // "create rules" follows the selection: selecting rows of the list keeps it open
    const selecting = drawer?.querySelector("[data-create-rules]")
                      && event.target.closest("[data-select-form] tbody, [data-select-all]");
    if (isOpen() && event.target.isConnected && !event.target.closest("#drawer")
        && !event.target.closest('[hx-target="#drawer"]') && !event.target.closest("[data-keeps-drawer]")
        && !selecting && !wasInside) {
      // a row of a list: a double-click on it opens it in the drawer (table.js). Its first click doesn't close the
      // drawer at once, else it would close and open again; the second click, or the row's view, keeps it
      if (event.target.closest("table[data-table] tbody tr, [data-select-form] tbody tr")) {
        cancelClose();
        if (event.detail > 1) return;
        closeTimer = setTimeout(() => {
          closeTimer = null;
          if (isOpen()) closeDrawer();
        }, DOUBLE_CLICK_MS);
      } else {
        closeDrawer();
      }
    }
  });

  // a view asked for meanwhile (the double-click, a link): the drawer stays open for it
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.target === getDrawer()) cancelClose();
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

  // the view shown again from the server (HX-Trigger drawerReload, checks.js when a check ended)
  function reload() {
    const drawer = getDrawer();
    if (isOpen()) reloadDrawerView(drawer);
  }
  document.addEventListener("drawerReload", reload);

  // The width of the drawer: its left edge dragged, or ←/→ on it. One width for every view, stored per browser; until
  // one is set, the details are narrower than the forms and package rules (drawer.css). Double-click (or Home) gives
  // the usual widths back.
  const WIDTH_KEY = "santa.drawer-width";

  function storedWidth() {
    try {
      const width = JSON.parse(localStorage.getItem(WIDTH_KEY));
      return typeof width === "number" ? width : null;
    } catch (error) {
      return null;
    }
  }

  function storeWidth(width) {
    try {
      if (width) localStorage.setItem(WIDTH_KEY, JSON.stringify(width));
      else localStorage.removeItem(WIDTH_KEY);
    } catch (error) {
      // private window or blocked storage: the width lasts until the next page
    }
  }

  function applyWidth(width) {
    const style = document.documentElement.style;
    ["--drawer-w", "--drawer-wide-w"].forEach((property) => {
      if (width) style.setProperty(property, `min(${width}rem, 100vw)`);
      else style.removeProperty(property);
    });
  }

  applyWidth(storedWidth());

  const MIN_WIDTH = 20;

  function setWidth(width) {
    const rem = SantaConsole.rem();
    const most = window.innerWidth / rem - 2;
    const value = Math.round(Math.max(MIN_WIDTH, Math.min(width, most)) * 10) / 10;
    applyWidth(value);
    // the tables of the drawer measure again (their scrollbar, the fit of their columns, table.js); not a resize of
    // the window: the snow would set up its canvas again at every step, and flicker
    document.dispatchEvent(new Event("drawerResized"));
    return value;
  }

  const currentWidth = () => getDrawer().getBoundingClientRect().width / SantaConsole.rem();

  function setUpResize(handle) {
    if (handle.dataset.ready) return;
    handle.dataset.ready = "1";
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      let width = currentWidth();
      handle.classList.add("dragging");
      document.body.classList.add("resizing-drawer");
      const move = (moveEvent) => {
        width = setWidth((window.innerWidth - moveEvent.clientX) / SantaConsole.rem());
      };
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        handle.classList.remove("dragging");
        document.body.classList.remove("resizing-drawer");
        storeWidth(width);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
    const reset = () => {
      applyWidth(null);
      storeWidth(null);
      document.dispatchEvent(new Event("drawerResized"));
    };
    handle.addEventListener("dblclick", reset);
    handle.addEventListener("keydown", (event) => {
      if (event.key === "Home") {
        event.preventDefault();
        reset();
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        // the edge moves with the arrow: left makes it wider
        const step = (event.shiftKey ? 4 : 1) * (event.key === "ArrowLeft" ? 1 : -1);
        storeWidth(setWidth(currentWidth() + step));
      }
    });
  }

  document.querySelectorAll("[data-drawer-resize]").forEach(setUpResize);
  document.addEventListener("htmx:afterSettle", () => document.querySelectorAll("[data-drawer-resize]").forEach(setUpResize));

  SantaConsole.drawer = { isOpen, close: closeDrawer, reload };
})();
