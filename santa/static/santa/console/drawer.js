// The drawer (#drawer): details and forms. A link inside it opens the next view in it; the views before are kept
// (their DOM, with what was entered) and "Back" brings them back.
(function () {
  "use strict";

  const drawerStack = [];
  let drawerSaved = false;

  function getDrawer() {
    return document.getElementById("drawer");
  }

  function isOpen() {
    return !!getDrawer()?.classList.contains("open");
  }

  function closeDrawer() {
    const drawer = getDrawer();
    // the drawer saved something the page behind it doesn't show yet
    if (drawerSaved || drawer.querySelector("[data-refresh-on-close]")) {
      window.location.reload();
      return;
    }
    drawerStack.length = 0;
    drawer.classList.remove("open");
    drawer.replaceChildren();
  }

  function pushDrawerView(drawer) {
    const view = document.createDocumentFragment();
    const scroll = drawer.scrollTop;
    while (drawer.firstChild) view.append(drawer.firstChild);
    drawerStack.push({ view, scroll });
  }

  function updateDrawerBack(drawer) {
    drawer.querySelectorAll("[data-drawer-back]").forEach((button) => button.remove());
    // the links back of the templates are for the page, the stack knows the way back
    drawer.querySelectorAll(".drawer-head .back").forEach((link) => {
      link.hidden = drawerStack.length > 0;
    });
    const tools = drawer.querySelector(".drawer-tools");
    const template = document.getElementById("drawer-back");
    if (drawerStack.length && tools && template) tools.prepend(template.content.cloneNode(true));
  }

  function drawerBack() {
    const drawer = getDrawer();
    const previous = drawerStack.pop();
    if (!previous) return;
    drawer.replaceChildren(previous.view);
    drawer.scrollTop = previous.scroll;
    updateDrawerBack(drawer);
    drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
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

  document.addEventListener("htmx:beforeSwap", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer || !event.detail.shouldSwap) return;
    const config = event.detail.requestConfig || {};
    if (config.headers?.["X-Drawer-Reload"]) return;
    const inside = config.elt && config.elt !== drawer && drawer.contains(config.elt);
    if (config.verb === "get" && inside && drawer.classList.contains("open")) {
      pushDrawerView(drawer);
    } else if (!inside) {
      // opened from the page: a new start
      drawerStack.length = 0;
    }
  });

  // after core.js opened the drawer and set up the new view
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = getDrawer();
    if (event.detail.target !== drawer) return;
    updateDrawerBack(drawer);
    drawer.querySelector("[data-drawer-title]")?.focus({ preventScroll: true });
  });

  // drawer_done(): back to the view before and update it, or reload the page when there is none
  document.addEventListener("drawerSaved", () => {
    const drawer = getDrawer();
    drawerSaved = true;
    if (!drawerStack.length) {
      window.location.reload();
      return;
    }
    drawerBack();
    reloadDrawerView(drawer);
  });

  document.addEventListener("click", (event) => {
    // × closes the drawer; "Cancel" of a view opened from another one goes back to it
    const close = event.target.closest("[data-drawer-close]");
    if (close && drawerStack.length && !close.closest(".drawer-tools")) drawerBack();
    else if (close) closeDrawer();
    if (event.target.closest("[data-drawer-back]")) drawerBack();
    // a click next to the open drawer closes it, except on what opens another one
    const drawer = getDrawer();
    // "create rules" follows the selection: selecting rows of the list keeps it open
    const selecting = drawer?.querySelector("[data-create-rules]")
                      && event.target.closest("[data-select-form] tbody, [data-select-all]");
    if (drawer?.classList.contains("open") && event.target.isConnected && !event.target.closest("#drawer")
        && !event.target.closest('[hx-target="#drawer"]') && !selecting) {
      closeDrawer();
    }
  });

  // Escape closes the drawer, and only the drawer: table.js would clear the selection behind it
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isOpen()) return;
    event.stopImmediatePropagation();
    closeDrawer();
  });

  SantaConsole.drawer = { isOpen, close: closeDrawer };
})();
