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
    // htmx gives a new element the class and style of the old one with its id, and puts its own back ~20 ms later:
    // that undid what the setup after the swap set (the height of a textarea after "Open as page", a menu's place)
    htmx.config.attributesToSettle = [];
  }
  document.addEventListener("htmx:historyRestore", () => {
    applyProgress();
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
  // The bar is hidden a moment after the last end: a page and the request of its rows right after show one bar.
  let running = 0;
  let visible = false;
  let showTimer = null;
  let hideTimer = null;

  function progressBar() {
    return document.getElementById("progress");
  }

  // the share the bar shows (a number), or null for the moving stripe: kept here, so that the bar of a page swapped in
  // (another tab while a check runs) goes on where it was
  let barShare = null;
  // the steps of a watched request are shown (with their total): an upload of another request doesn't change the bar
  let stepsShown = false;

  function setShare(share) {
    barShare = share;
    const bar = progressBar();
    if (!bar) return;
    bar.classList.toggle("determinate", share !== null);
    if (share === null) bar.style.removeProperty("--progress");
    else bar.style.setProperty("--progress", `${share}%`);
  }

  // also on the bar of a page swapped in, which comes hidden
  function applyProgress() {
    const bar = progressBar();
    if (!bar) return;
    bar.hidden = !visible;
    if (!visible) barShare = null;
    setShare(barShare);
  }

  function startProgress() {
    running += 1;
    clearTimeout(hideTimer);
    if (running === 1 && !visible) {
      clearTimeout(showTimer);
      showTimer = setTimeout(() => {
        visible = true;
        applyProgress();
      }, 150);
    }
  }

  function stopProgress() {
    running = Math.max(0, running - 1);
    if (running) return;
    stopWatching();
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      visible = false;
      applyProgress();
    }, 150);
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

  // The steps of a long request (form[data-progress]: checking package rules, unpacking an upload, saving many
  // rules): the form sends a new ID in progress_id, the server writes how far it is (santa/progress.py), the page
  // asks for it while it waits and shows "3 / 12 · name" under the bar. Ends with the request, or with the page.
  const PROGRESS_PLACEHOLDER = "00000000-0000-0000-0000-000000000000";
  let watchTimer = null;
  let watching = null;

  function newProgressId() {
    // randomUUID only exists on HTTPS and localhost
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const hex = Array.from(window.crypto.getRandomValues(new Uint8Array(16)),
      (byte) => byte.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  function progressStatus() {
    return document.getElementById("progress-status");
  }

  function showStep(data) {
    const bar = progressBar();
    const status = progressStatus();
    if (!bar || !status || (!data.current && !data.label)) return;
    const parts = [];
    if (data.current && data.total) {
      parts.push(status.dataset.of.replace("%(current)s", data.current).replace("%(total)s", data.total));
    } else if (data.current) {
      parts.push(status.dataset.found.replace("%(count)s", data.current));
    }
    if (data.label) parts.push(data.label);
    status.textContent = parts.join(" · ");
    status.hidden = false;
    // without a total (the upload is sent, the server works on it): the moving stripe again
    stepsShown = Boolean(data.total);
    setShare(data.total ? Math.max(2, Math.round((data.current - 1) / data.total * 100)) : null);
  }

  // Is the request still alive? The server refreshes its entry every 10 s while it works (santa/progress.py).
  // No heartbeat for this long while the server answers: it crashed, or a worker was killed or restarted.
  const PROGRESS_DEAD_AFTER = 35;
  // the htmx request of a progress ID (htmx:beforeSend), to abort it when it died
  const progressRequests = new Map();

  function watchProgress(id) {
    stopWatching();
    const url = progressStatus()?.dataset.url?.replace(PROGRESS_PLACEHOLDER, id);
    if (!url) return;
    const current = { id, failures: 0 };
    watching = current;
    const poll = async () => {
      let data = null;
      try {
        const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" },
                                            signal: AbortSignal.timeout(10000) });
        if (!response.ok) throw new Error(String(response.status));
        data = await response.json();
      } catch {
        // twice in a row: is the server still there? (the banner of the connection, the request is aborted when
        // it comes back: see recovered)
        current.failures += 1;
        if (current.failures === 2) SantaConsole.connection?.suspect();
      }
      if (watching !== current) return;
      if (data) {
        current.failures = 0;
        showStep(data);
        if (progressDied(current, data)) {
          died(current);
          return;
        }
      }
      watchTimer = setTimeout(poll, 700);
    };
    watchTimer = setTimeout(poll, 400);
  }

  // No entry yet is no sign of death: a request can work a while before its progress starts (the catalogs, an upload
  // unpacked); a connection that is gone shows in the banner of the connection
  function progressDied(current, data) {
    if (data.done) return false;
    return data.beat_age != null && data.beat_age > PROGRESS_DEAD_AFTER;
  }

  function died(current) {
    const status = progressStatus();
    const message = status?.dataset.dead || "No answer from the server: the action was stopped.";
    const xhr = progressRequests.get(current.id);
    stopWatching();
    addToast("error", message);
    if (xhr) {
      // its loadend clears the bar and the busy button
      xhr.abort();
      return;
    }
    // a form that loads a page: the browser waits for it, stop that
    window.stop();
    running = 0;
    visible = false;
    applyProgress();
    document.querySelectorAll('[aria-busy="true"]').forEach((button) => setBusy(button, false));
  }

  document.addEventListener("htmx:beforeSend", (event) => {
    const config = event.detail.requestConfig || {};
    const id = config.formData?.get?.("progress_id") || config.parameters?.progress_id;
    const xhr = event.detail.xhr;
    if (!id || !xhr) return;
    progressRequests.set(id, xhr);
    xhr.addEventListener("loadend", () => progressRequests.delete(id), { once: true });
  });

  function stopWatching() {
    watching = null;
    clearTimeout(watchTimer);
    stepsShown = false;
    setShare(null);
    const status = progressStatus();
    if (status) {
      status.hidden = true;
      status.textContent = "";
    }
  }

  // a button that sends its form with htmx itself (the preview of a package rule)
  document.addEventListener("htmx:configRequest", (event) => {
    const elt = event.detail.elt;
    if (elt?.tagName === "FORM" || !elt?.matches?.("[data-progress]")) return;
    const id = newProgressId();
    event.detail.parameters.progress_id = id;
    setTimeout(() => watchProgress(id));
  });

  // the button that sent a form, for the request htmx makes of it
  let submitter = null;
  document.addEventListener("submit", (event) => {
    submitter = event.submitter || null;
    const form = event.target;
    if (form.matches?.("[data-progress]")) {
      // before htmx reads the form: the field goes with the request
      let field = form.querySelector('input[name="progress_id"]');
      if (!field) {
        field = document.createElement("input");
        field.type = "hidden";
        field.name = "progress_id";
        form.append(field);
      }
      field.value = newProgressId();
      setTimeout(() => {
        if (!event.defaultPrevented || form.matches("[hx-post], [data-hx-post]")) watchProgress(field.value);
      });
    }
    // not taken over by htmx or a script: the browser loads the next page
    setTimeout(() => {
      if (event.defaultPrevented) return;
      startProgress();
      setBusy(event.submitter, true);
    });
  }, true);
  document.addEventListener("htmx:beforeRequest", (event) => {
    const elt = event.detail.elt;
    if (elt?.tagName === "FORM" && submitter && elt.contains(submitter)) {
      event.detail.requestConfig.busyButton = submitter;
    }
    submitter = null;
  });
  // Counted from the send (a request cancelled before never ends) to the end of its XHR: htmx:afterRequest doesn't
  // reach the document when the element left the page meanwhile (its row replaced, the drawer view kept aside)
  document.addEventListener("htmx:beforeSend", (event) => {
    const xhr = event.detail.xhr;
    if (!xhr) return;
    const button = event.detail.requestConfig?.busyButton;
    startProgress();
    setBusy(button, true);
    // an upload: the share sent so far. Only the upload (htmx reports the download of every answer too, the HTML of
    // another tab filled the bar), and not over the steps of a long request
    xhr.upload?.addEventListener("progress", (progress) => {
      if (!progress.lengthComputable || !progress.total || progress.loaded >= progress.total) return;
      if (stepsShown) return;
      setShare(Math.round(progress.loaded / progress.total * 100));
    });
    xhr.addEventListener("loadend", () => {
      stopProgress();
      setBusy(button, false);
    }, { once: true });
  });
  // Back to a page kept by the browser: nothing runs on it
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    running = 0;
    visible = false;
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    applyProgress();
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
        if (total) setShare(Math.round(loaded / total * 100));
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

  // The messages of a request (ul[data-toasts] of the page or the drawer): shown for a moment at the bottom, the bell
  // in the top bar keeps them (message_storage.py). Errors stay longer; hovering or focusing one holds it.
  function closeToast(toast) {
    if (!toast.isConnected || toast.classList.contains("leaving")) return;
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 200);
  }

  // At most this many at a time: many messages at once (a check that found many versions) are one more toast that
  // leads to the bell. An error takes the place of the oldest other one.
  const MAX_TOASTS = 3;
  let moreToast = null;
  let moreCount = 0;

  function showMore(box) {
    moreCount += 1;
    if (!moreToast?.isConnected) {
      moreToast = document.createElement("div");
      moreToast.className = "toast info toast-more";
      const link = document.createElement("a");
      link.className = "toast-text";
      link.href = box.dataset.moreUrl || "/notifications/";
      moreToast.append(link);
      box.append(moreToast);
      setTimeout(() => {
        closeToast(moreToast);
        moreCount = 0;
      }, 8000);
    }
    moreToast.querySelector("a").textContent = moreCount === 1 ? box.dataset.moreOne || "1 more message"
      : (box.dataset.more || "%(count)s more messages").replace("%(count)s", moreCount);
  }

  // level: success, info, warning, error
  function addToast(level, message) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const shown = [...box.querySelectorAll(".toast:not(.leaving):not(.toast-more)")];
    if (shown.length >= MAX_TOASTS) {
      const other = level === "error" && shown.find((toast) => !toast.classList.contains("error"));
      if (!other) {
        showMore(box);
        return;
      }
      other.remove();
      showMore(box);
    }
    const toast = document.createElement("div");
    toast.className = `toast ${level}`;
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "icon-button small";
    close.setAttribute("aria-label", box.dataset.closeLabel || "Close");
    close.textContent = "×";
    close.addEventListener("click", () => closeToast(toast));
    toast.append(text, close);
    // the "more" one stays the last
    box.insertBefore(toast, moreToast?.isConnected ? moreToast : null);
    let timer = null;
    const start = () => {
      timer = setTimeout(() => closeToast(toast), level === "error" ? 12000 : 6000);
    };
    const hold = () => clearTimeout(timer);
    toast.addEventListener("mouseenter", hold);
    toast.addEventListener("focusin", hold);
    toast.addEventListener("mouseleave", start);
    toast.addEventListener("focusout", start);
    start();
  }

  function showToasts(root) {
    root.querySelectorAll?.("ul[data-toasts]").forEach((list) => {
      list.querySelectorAll("li").forEach((item) => {
        addToast(["success", "warning", "error"].find((name) => item.classList.contains(name)) || "info",
                 item.textContent);
      });
      list.remove();
    });
  }
  SantaConsole.onInit(showToasts);
  SantaConsole.onSwap(showToasts);
  // the messages of an answer that is not shown (drawer.js: its view was left meanwhile)
  SantaConsole.showToastsOf = (html) => {
    if (html.includes("data-toasts")) showToasts(new DOMParser().parseFromString(html, "text/html"));
  };
  SantaConsole.addToast = addToast;

  // The bell: the count of the unread messages, again after every request that added one (HX-Trigger
  // notificationsChanged); the open menu loads its list again
  function notificationsMenu() {
    return document.querySelector("[data-notifications-menu]");
  }

  function setUnread(count) {
    const summary = notificationsMenu()?.querySelector("summary");
    const badge = summary?.querySelector("[data-unread-count]");
    if (!badge) return;
    badge.textContent = String(count);
    badge.hidden = !count;
    summary.setAttribute("aria-label", count
      ? summary.dataset.labelUnread.replace("%(count)s", count) : summary.dataset.label);
  }

  document.addEventListener("notificationsChanged", async () => {
    const menu = notificationsMenu();
    if (!menu) return;
    const summary = menu.querySelector("summary");
    if (menu.open && window.htmx) {
      window.htmx.ajax("GET", summary.getAttribute("hx-get"), { target: "[data-notifications-panel]" });
      return;
    }
    try {
      const response = await fetch(summary.dataset.unreadUrl, { credentials: "same-origin" });
      if (response.ok) setUnread((await response.json()).unread);
    } catch {
      // the next page shows it
    }
  });
  // opening the list marks them as read
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target?.matches?.("[data-notifications-panel]")) setUnread(0);
  });
  // only a request when it opens, not when it closes
  document.addEventListener("htmx:confirm", (event) => {
    const menu = event.detail.elt?.closest?.("[data-notifications-menu]");
    if (menu && event.detail.elt.matches("summary") && menu.open) event.preventDefault();
  });

  document.addEventListener("DOMContentLoaded", () => SantaConsole.init(document));
  document.addEventListener("htmx:afterSwap", (event) => {
    const drawer = document.getElementById("drawer");
    // a page restored by Back / Forward has no target here (htmx:historyRestore sets it up)
    if (!event.detail.target) return;
    if (event.detail.target === document.body) {
      // a boosted page: set up like a loaded one
      applyProgress();
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
  // Placed before it shows (beforetoggle): toggle comes after the first paint, the menu flashed at its old place.
  let menuButton = null;
  document.addEventListener("click", (event) => {
    menuButton = event.target.closest?.("[popovertarget]") || null;
  }, true);
  document.addEventListener("beforetoggle", (event) => {
    const menu = event.target;
    if (!menu.matches?.(".row-menu") || event.newState !== "open") return;
    // the button clicked, not just the first one in the page that names the menu
    const button = [menuButton, event.source].find((element) => element?.getAttribute?.("popovertarget") === menu.id)
                   || document.querySelector(`[popovertarget="${CSS.escape(menu.id)}"]`);
    menuButton = null;
    if (!button) return;
    // not shown yet: measured as it will be, without being seen
    menu.style.display = "grid";
    menu.style.visibility = "hidden";
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.removeProperty("display");
    menu.style.removeProperty("visibility");
    const box = button.getBoundingClientRect();
    const below = box.bottom + 4 + height <= window.innerHeight;
    menu.style.left = `${Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8))}px`;
    menu.style.top = `${below ? box.bottom + 4 : Math.max(8, box.top - 4 - height)}px`;
  }, true);
  document.addEventListener("toggle", (event) => {
    const menu = event.target;
    if (!menu.matches?.(".row-menu") || event.newState !== "open") return;
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
    // the user menu and the bell: a click elsewhere closes them, one opens without the other
    for (const selector of ["[data-user-menu]", "[data-notifications-menu]"]) {
      const menu = document.querySelector(selector);
      if (menu?.open && !event.target.closest(selector)) menu.open = false;
    }
    const copyButton = event.target.closest("[data-copy-text]");
    if (copyButton) copy(copyButton.dataset.copyText, copyButton);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const selector of ["[data-user-menu]", "[data-notifications-menu]"]) {
      const menu = document.querySelector(selector);
      if (menu?.open) {
        menu.open = false;
        menu.querySelector("summary").focus();
        // only the menu: the drawer behind it stays (drawer.js)
        event.stopImmediatePropagation();
      }
    }
    document.querySelector("[data-topbar]")?.classList.remove("nav-open");
  });
})();
