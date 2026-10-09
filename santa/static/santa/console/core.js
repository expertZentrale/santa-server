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

    // a textarea as high as its text: its rows are the minimum, 20rem the most it grows by itself (then it scrolls).
    // A height dragged by hand (data-user-height, forms.js) is kept and may go beyond that. A hidden one is fitted
    // when it is shown.
    fitTextarea(textarea) {
      if (!textarea.offsetParent) return;
      const style = getComputedStyle(textarea);
      textarea.style.height = "auto";
      const borders = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      const content = textarea.scrollHeight + borders;
      const dragged = parseFloat(textarea.dataset.userHeight) || 0;
      const height = Math.max(dragged, Math.min(content, 20 * SantaConsole.rem()));
      textarea.style.height = `${height}px`;
      textarea.dataset.fitHeight = height;
    },
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

  // Pages switch without loading the whole page again (hx-boost on the body): htmx swaps the body. Back and Forward
  // get the page from the server again (no copy kept: its scripts would not be set up), in place too.
  if (window.htmx) {
    htmx.config.historyCacheSize = 0;
    htmx.config.refreshOnHistoryMiss = false;
  }
  document.addEventListener("htmx:historyRestore", () => {
    SantaConsole.init(document.body);
    document.dispatchEvent(new CustomEvent("santa:page"));
  });

  // Pages of another layout (the Django admin, the sign-in of the provider) load as a whole page
  const OTHER_LAYOUTS = /^\/(admin|oidc)\//;
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (!event.detail.boosted) return;
    const url = new URL(event.detail.requestConfig.path, window.location.href);
    if (url.origin === window.location.origin && !OTHER_LAYOUTS.test(url.pathname)) return;
    event.preventDefault();
    window.location.href = url.toString();
  });
  // a boosted link or form is a page: the body, also inside an element that targets the drawer
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.boosted && event.detail.target !== document.body) event.detail.target = document.body;
  });

  // the attributes of <html> of the new page: language, theme, season (a body swap leaves them)
  // and of <body>: hx-headers has the CSRF token
  const HTML_ATTRIBUTES = ["lang", "data-theme", "data-season"];
  const BODY_ATTRIBUTES = ["hx-headers"];
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target !== document.body || !event.detail.shouldSwap) return;
    const response = event.detail.serverResponse || "";
    const html = /<html[^>]*>/i.exec(response);
    const body = /<body[^>]*>/i.exec(response);
    if (!html) return;
    const parsed = new DOMParser().parseFromString(`${html[0]}${body ? body[0] : ""}</body></html>`, "text/html");
    HTML_ATTRIBUTES.forEach((name) => {
      if (parsed.documentElement.hasAttribute(name)) {
        document.documentElement.setAttribute(name, parsed.documentElement.getAttribute(name));
      } else {
        document.documentElement.removeAttribute(name);
      }
    });
    BODY_ATTRIBUTES.forEach((name) => {
      if (body && parsed.body.hasAttribute(name)) document.body.setAttribute(name, parsed.body.getAttribute(name));
    });
  });

  // Theme and language of the user menu (form[data-preference]): saved in the background. The theme is applied right
  // away; a language needs the texts of the server, so the page is rendered again in place and the menu opens again.
  let reopenMenu = null;
  document.addEventListener("click", (event) => {
    const button = event.target.closest("form[data-preference] button[name]");
    if (!button || !window.fetch) return;
    event.preventDefault();
    const form = button.form;
    const data = new FormData(form);
    data.set(button.name, button.value);
    form.querySelectorAll("button[name]").forEach((other) => {
      other.setAttribute("aria-pressed", other === button ? "true" : "false");
    });
    if (button.name === "theme") document.documentElement.dataset.theme = button.value;
    const saved = fetch(form.action, { method: "POST", body: data, credentials: "same-origin",
                                       headers: { "X-Santa-Preference": "1" } });
    if (button.name !== "language") return;
    saved.then((response) => {
      if (!response.ok) return;
      reopenMenu = button.value;
      const scroll = window.scrollY;
      htmx.ajax("GET", window.location.href, {
        target: document.body, swap: "innerHTML show:none",
        headers: { "HX-Boosted": "true", "X-Santa-Rerender": "1" },
      }).then(() => window.scrollTo(0, scroll));
    });
  });
  document.addEventListener("santa:page", () => {
    if (reopenMenu === null) return;
    const menu = document.querySelector("[data-user-menu]");
    if (menu) {
      menu.open = true;
      menu.querySelector(`button[name="language"][value="${reopenMenu}"]`)?.focus();
    }
    reopenMenu = null;
  });

  // The progress bar (#progress): something runs. htmx requests (shown after a moment: quick ones don't flash), forms
  // that load a page, uploads and downloads (with their share).
  let running = 0;
  let showTimer = null;

  function progressBar() {
    return document.getElementById("progress");
  }

  function startProgress() {
    running += 1;
    if (running === 1) showTimer = setTimeout(() => {
      const bar = progressBar();
      if (bar) bar.hidden = false;
    }, 150);
  }

  function stopProgress() {
    running = Math.max(0, running - 1);
    if (running) return;
    clearTimeout(showTimer);
    const bar = progressBar();
    if (!bar) return;
    bar.hidden = true;
    bar.classList.remove("determinate");
    bar.style.removeProperty("--progress");
  }

  function setBusy(button, busy) {
    if (!button?.matches?.(".button, button")) return;
    if (busy) {
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
    } else {
      button.removeAttribute("aria-busy");
      button.disabled = false;
    }
  }

  // the button that sent a form, for the request htmx makes of it
  let submitter = null;
  document.addEventListener("submit", (event) => {
    submitter = event.submitter || null;
    // not taken over by htmx or a script: the browser loads the next page
    setTimeout(() => {
      if (event.defaultPrevented) return;
      startProgress();
      setBusy(event.submitter, true);
    });
  }, true);
  document.addEventListener("htmx:beforeRequest", (event) => {
    startProgress();
    const elt = event.detail.elt;
    if (elt?.tagName === "FORM" && submitter && elt.contains(submitter)) {
      event.detail.requestConfig.busyButton = submitter;
      setBusy(submitter, true);
    }
    submitter = null;
  });
  // htmx sends afterRequest for every end (also errors, aborts, timeouts), and a second time on a parent when the
  // element left the page with the swap: one stop per request
  const finished = new WeakSet();
  document.addEventListener("htmx:afterRequest", (event) => {
    const xhr = event.detail?.xhr;
    if (xhr && finished.has(xhr)) return;
    if (xhr) finished.add(xhr);
    stopProgress();
    setBusy(event.detail?.requestConfig?.busyButton, false);
  });
  // an upload: the share sent so far
  document.addEventListener("htmx:xhr:progress", (event) => {
    const { loaded, total } = event.detail;
    const bar = progressBar();
    if (!bar || !total || loaded >= total) return;
    bar.classList.add("determinate");
    bar.style.setProperty("--progress", `${Math.round(loaded / total * 100)}%`);
  });
  // Back to a page kept by the browser: nothing runs on it
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    running = 1;
    stopProgress();
    document.querySelectorAll('.button[aria-busy="true"]').forEach((button) => setBusy(button, false));
  });

  // A download (a[data-download]): fetched in the page, with its progress, then saved from memory. The page stays.
  function showError(text) {
    let list = document.querySelector("main .messages");
    if (!list) {
      list = document.createElement("ul");
      list.className = "messages";
      document.querySelector("main")?.prepend(list);
    }
    const item = document.createElement("li");
    item.className = "error";
    item.textContent = text;
    list.append(item);
  }

  function fileName(response, url) {
    const header = response.headers.get("Content-Disposition") || "";
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
    return match ? decodeURIComponent(match[1]) : url.pathname.split("/").filter(Boolean).pop() || "download";
  }

  async function download(link) {
    const url = new URL(link.href, window.location.href);
    const bar = progressBar();
    startProgress();
    setBusy(link, true);
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const total = Number(response.headers.get("Content-Length")) || 0;
      const reader = response.body.getReader();
      const chunks = [];
      let loaded = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        loaded += value.length;
        if (total && bar) {
          bar.classList.add("determinate");
          bar.style.setProperty("--progress", `${Math.round(loaded / total * 100)}%`);
        }
      }
      const blob = new Blob(chunks, { type: response.headers.get("Content-Type") || "application/octet-stream" });
      const save = document.createElement("a");
      save.href = URL.createObjectURL(blob);
      save.download = fileName(response, url);
      document.body.append(save);
      save.click();
      save.remove();
      setTimeout(() => URL.revokeObjectURL(save.href), 10000);
    } catch (error) {
      showError(link.dataset.downloadError || document.getElementById("progress")?.dataset.downloadError
                || "The download failed.");
    } finally {
      stopProgress();
      link.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-download]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey
        || !window.fetch || !window.ReadableStream) return;
    event.preventDefault();
    if (link.getAttribute("aria-busy") !== "true") download(link);
  });

  document.addEventListener("DOMContentLoaded", () => SantaConsole.init(document));
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = document.getElementById("drawer");
    // a page restored by Back / Forward has no target here (htmx:historyRestore sets it up)
    if (!event.detail.target) return;
    if (event.detail.target === document.body) {
      // a boosted page: set up like a loaded one
      SantaConsole.init(document.body);
      document.dispatchEvent(new CustomEvent("santa:page"));
    } else if (event.detail.target === drawer) {
      drawer.classList.add("open");
      SantaConsole.init(drawer);
    } else if (event.detail.target.matches?.("[data-list-rows]")) {
      // the rows of a list, loaded after the page (console/_rows.html)
      SantaConsole.init(event.detail.target);
    }
    swapHooks.forEach((fn) => fn(event.detail.target));
  });
  // the rows could not be loaded: say so instead of loading forever
  document.addEventListener("htmx:responseError", (event) => failRows(event.detail.elt));
  document.addEventListener("htmx:sendError", (event) => failRows(event.detail.elt));

  // The connection to the server (#connection). Lost: a network error, or /health not answering twice. Behind a
  // proxy or a forwarded port a stopped server doesn't refuse connections, requests just hang: a page request without
  // an answer for a while makes the console check /health. The times follow the measured latency, so a slow network
  // (3G) is no outage. Back (any answer): the requests still hanging are aborted (they hold the browser's few
  // connections, everything else waits behind them), the failed GETs are repeated; a POST is not sent again, it may
  // have changed something.
  const inflight = new Map();
  let connectionLost = false;
  let checkTimer = null;
  let failedGets = [];
  let failedPost = false;
  let bannerTimer = null;
  // round trip of the answers, a running average (ms); to start, the browser's own estimate of the network
  let latency = Math.max(300, Number(navigator.connection?.rtt) || 0);
  // the last answer of the server: one during a check shows that it is there, also when /health was slower
  let answeredAt = 0;

  function measured(ms) {
    latency = latency * 0.8 + Math.min(ms, 20000) * 0.2;
    answeredAt = Date.now();
  }
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const healthTimeout = () => clamp(4 * latency + 1500, 3000, 15000);
  const hangingAfter = () => clamp(3 * latency + 2000, 3000, 12000);

  function banner(text, state) {
    const element = document.getElementById("connection");
    if (!element) return;
    clearTimeout(bannerTimer);
    element.querySelector("span").textContent = text;
    element.dataset.state = state;
    element.hidden = false;
    if (state !== "lost") bannerTimer = setTimeout(() => { element.hidden = true; }, state === "back" ? 2500 : 8000);
  }

  async function serverAnswers() {
    const started = Date.now();
    try {
      const response = await fetch("/health", { cache: "no-store", credentials: "omit",
                                                 signal: AbortSignal.timeout(healthTimeout()) });
      if (response.ok) measured(Date.now() - started);
      return response.ok;
    } catch (error) {
      return false;
    }
  }

  function lost(entry) {
    if (entry?.verb === "get") failedGets.push(entry);
    else if (entry) failedPost = true;
    if (connectionLost) return;
    connectionLost = true;
    banner(document.getElementById("connection")?.dataset.lost || "Connection lost. Retrying…", "lost");
    scheduleCheck(500);
  }

  function scheduleCheck(delay) {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(async () => {
      if (!connectionLost) return;
      if (await serverAnswers()) recovered();
      else scheduleCheck(1000);
    }, delay);
  }

  function repeat(entry) {
    const elt = entry.elt;
    if (!elt?.isConnected) return;
    if (entry.boosted && elt.tagName === "A") elt.click();
    else if (entry.boosted && elt.tagName === "FORM") elt.requestSubmit();
    else htmx.ajax("GET", entry.path, { source: elt, target: entry.target?.isConnected ? entry.target : elt });
  }

  function recovered() {
    if (!connectionLost) return;
    connectionLost = false;
    clearTimeout(checkTimer);
    // still hanging from before: abort them, their GETs are repeated below
    [...inflight].forEach(([xhr, entry]) => {
      inflight.delete(xhr);
      if (entry.verb === "get") failedGets.push(entry);
      else failedPost = true;
      xhr.abort();
    });
    // once per element; of the pages, only the last one asked for
    const byElement = new Map();
    failedGets.forEach((entry) => byElement.set(entry.elt, entry));
    const entries = [...byElement.values()];
    const page = entries.filter((entry) => entry.boosted).pop();
    const element = document.getElementById("connection");
    banner(failedPost ? element?.dataset.notSaved || "" : element?.dataset.back || "", failedPost ? "not-saved" : "back");
    failedGets = [];
    failedPost = false;
    // the rows of a list: their placeholder loads them again below, once
    entries.filter((entry) => entry.rows).forEach((entry) => failRows(entry.elt));
    entries.filter((entry) => !entry.boosted && !entry.rows).forEach(repeat);
    if (page) repeat(page);
    // rows that showed "could not be loaded": loaded again
    document.querySelectorAll(".rows-loading.failed").forEach((placeholder) => {
      placeholder.classList.remove("failed");
      placeholder.setAttribute("aria-busy", "true");
      placeholder.querySelector(".rows-error")?.setAttribute("hidden", "");
      htmx.ajax("GET", placeholder.getAttribute("hx-get"), {
        source: placeholder, target: placeholder.closest("[data-list-rows]"), headers: { "X-Santa-Rows": "1" },
      });
    });
  }

  document.addEventListener("htmx:beforeSend", (event) => {
    const config = event.detail.requestConfig || {};
    const headers = config.headers || {};
    inflight.set(event.detail.xhr, {
      elt: event.detail.elt, verb: (config.verb || "get").toLowerCase(), path: config.path, target: event.detail.target,
      boosted: Boolean(event.detail.boosted || config.boosted), rows: headers["X-Santa-Rows"] === "1",
      started: Date.now(),
    });
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    const xhr = event.detail.xhr;
    const entry = inflight.get(xhr);
    inflight.delete(xhr);
    if (!entry) return;
    if (xhr.status === 0) {
      // no answer at all (a network error; aborted ones left the list before): the server is gone
      lost(entry);
      return;
    }
    measured(Date.now() - entry.started);
    // an answer: the server is there
    if (connectionLost) recovered();
  });
  document.addEventListener("htmx:sendAbort", (event) => inflight.delete(event.detail.xhr));

  // a request without an answer in time: /health is asked, twice before the connection counts as lost
  let suspecting = false;
  async function suspect() {
    if (connectionLost || suspecting) return true;
    suspecting = true;
    const since = Date.now();
    let answers = await serverAnswers() || answeredAt > since;
    if (!answers && !connectionLost) answers = await serverAnswers() || answeredAt > since;
    suspecting = false;
    if (!answers) lost(null);
    return answers;
  }

  // The server answers, but a request still waits. A page or a drawer view answers quickly: it hangs on a connection
  // the server dropped (a restart) and never will, so it is asked again at once. The rows of a list can be slow on a
  // big list: only after 12 s. Once per address and minute.
  const checked = new WeakSet();
  const checkedLong = new WeakSet();
  const repeatedAt = new Map();
  setInterval(async () => {
    if (connectionLost || document.hidden) return;
    const now = Date.now();
    const gets = [...inflight].filter(([, entry]) => entry.verb === "get");
    const waiting = gets.filter(([xhr, entry]) => now - entry.started > hangingAfter() && !checked.has(xhr));
    const long = gets.filter(([xhr, entry]) => entry.rows && now - entry.started > 12000 && !checkedLong.has(xhr));
    if (!waiting.length && !long.length) return;
    waiting.forEach(([xhr]) => checked.add(xhr));
    long.forEach(([xhr]) => checkedLong.add(xhr));
    if (!(await suspect())) return;
    const later = Date.now();
    waiting.filter(([, entry]) => !entry.rows).concat(long)
      .filter(([xhr, entry]) => inflight.has(xhr) && later - (repeatedAt.get(entry.path) || 0) > 60000)
      .forEach(([xhr, entry]) => {
        inflight.delete(xhr);
        repeatedAt.set(entry.path, later);
        xhr.abort();
        repeat(entry);
      });
  }, 1000);
  document.addEventListener("click", (event) => {
    const element = event.target.closest("#connection");
    if (element && element.dataset.state !== "lost") element.hidden = true;
  });
  // lost: a network error; suspect: no answer in time, the server is checked first; timeout: for a request's own
  // time limit
  SantaConsole.connection = { lost: () => lost(null), suspect, isLost: () => connectionLost, timeout: healthTimeout };

  function failRows(element) {
    if (!element?.matches?.(".rows-loading")) return;
    element.classList.add("failed");
    element.removeAttribute("aria-busy");
    element.querySelector(".rows-error")?.removeAttribute("hidden");
  }

  // The ⋯ menu of a row (a popover, see table.css): under its button, right-aligned, above it when there is no room
  // below. Closed by a choice, a click elsewhere or Escape (the browser's popover).
  document.addEventListener("toggle", (event) => {
    const menu = event.target;
    if (!menu.matches?.(".row-menu") || event.newState !== "open") return;
    const button = document.querySelector(`[popovertarget="${CSS.escape(menu.id)}"]`);
    if (!button) return;
    const box = button.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const below = box.bottom + 4 + height <= window.innerHeight;
    menu.style.left = `${Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8))}px`;
    menu.style.top = `${below ? box.bottom + 4 : Math.max(8, box.top - 4 - height)}px`;
    menu.querySelector("a, button")?.focus({ preventScroll: true });
  }, true);
  document.addEventListener("click", (event) => {
    const item = event.target.closest(".row-menu .menu-item");
    if (item) item.closest(".row-menu").hidePopover?.();
  });
  // the page scrolls: an open menu would stay where it was
  window.addEventListener("scroll", () => {
    document.querySelectorAll(".row-menu:popover-open").forEach((menu) => menu.hidePopover());
  }, { passive: true });

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
