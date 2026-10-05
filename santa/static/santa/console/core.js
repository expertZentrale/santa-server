// The console pages: plain JS next to htmx, no build step. This file is loaded first: it holds the helpers the other
// files share (window.SantaConsole) and calls their setup when a page loads or htmx swaps content in.
(function () {
  "use strict";

  const initHooks = [];
  const swapHooks = [];

  const SantaConsole = {
    // fn(root): the page, or the drawer after a new view came in
    onInit(fn) {
      initHooks.push(fn);
    },
    // fn(target): after any htmx swap, also of a row or a part of a form
    onSwap(fn) {
      swapHooks.push(fn);
    },
    init(root) {
      initHooks.forEach((fn) => fn(root));
    },

    controlValue(form, name) {
      const controls = form.querySelectorAll(`[name="${name}"], [name$="-${name}"]`);
      for (const control of controls) {
        if (control.type === "radio") {
          if (control.checked) return control.value;
        } else if (control.type === "checkbox") {
          return control.checked ? "on" : "";
        } else {
          return control.value;
        }
      }
      return "";
    },

    debounce(fn, delay) {
      let timer;
      return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), delay);
      };
    },

    // ↑/↓ in a list of suggestions, shared by every combobox
    moveSelection(box, step) {
      const options = [...box.querySelectorAll(".suggestion:not([aria-disabled=true])")];
      if (!options.length) return;
      let index = options.findIndex((option) => option.getAttribute("aria-selected") === "true");
      options.forEach((option) => option.setAttribute("aria-selected", "false"));
      index = Math.min(Math.max(index + step, 0), options.length - 1);
      options[index].setAttribute("aria-selected", "true");
      options[index].scrollIntoView({ block: "nearest" });
      // screen readers follow the option through the input that controls the list
      if (box.id && options[index].id) {
        document.querySelector(`[aria-controls="${box.id}"]`)?.setAttribute("aria-activedescendant", options[index].id);
      }
    },

    rem: () => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16,
  };
  window.SantaConsole = SantaConsole;

  function copy(text, element) {
    const done = () => {
      element.classList.add("copied");
      setTimeout(() => element.classList.remove("copied"), 1200);
    };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text.trim()).then(done);
      return;
    }
    // the Clipboard API only exists on HTTPS and localhost
    const area = document.createElement("textarea");
    area.value = text.trim();
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    if (document.execCommand("copy")) done();
    area.remove();
  }

  // The server shows the times in the time zone of the browser (unless the profile sets one)
  function sendTimeZone() {
    let zone = "";
    try {
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      return;
    }
    // IANA names (Europe/Berlin) are valid cookie values as they are; Django doesn't URL-decode cookies
    if (!/^[A-Za-z0-9_+\-/]+$/.test(zone)) return;
    const current = document.cookie.split("; ").find((item) => item.startsWith("santa_tz="));
    if (current === `santa_tz=${zone}`) return;
    const secure = location.protocol === "https:" ? "; secure" : "";
    document.cookie = `santa_tz=${zone}; path=/; max-age=31536000; samesite=lax${secure}`;
  }

  sendTimeZone();
  document.addEventListener("DOMContentLoaded", () => SantaConsole.init(document));
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = document.getElementById("drawer");
    if (event.detail.target === drawer) {
      drawer.classList.add("open");
      SantaConsole.init(drawer);
    }
    swapHooks.forEach((fn) => fn(event.detail.target));
  });

  // the burger menu, the user menu and the copy buttons
  document.addEventListener("click", (event) => {
    const burger = event.target.closest("[data-burger]");
    if (burger) {
      const open = burger.closest("[data-topbar]").classList.toggle("nav-open");
      burger.setAttribute("aria-expanded", open ? "true" : "false");
    } else if (!event.target.closest("#main-nav")) {
      document.querySelector("[data-topbar]")?.classList.remove("nav-open");
      document.querySelector("[data-burger]")?.setAttribute("aria-expanded", "false");
    }
    const menu = document.querySelector("[data-user-menu]");
    if (menu?.open && !event.target.closest("[data-user-menu]")) menu.open = false;
    const copyButton = event.target.closest("[data-copy-text]");
    if (copyButton) copy(copyButton.dataset.copyText, copyButton);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = document.querySelector("[data-user-menu]");
    if (menu?.open) {
      menu.open = false;
      menu.querySelector("summary").focus();
    }
    document.querySelector("[data-topbar]")?.classList.remove("nav-open");
  });
})();
