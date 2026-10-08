// New events without a reload: every 10 s the changed rows come from the server. A row that is already in the
// list is replaced (its checkbox stays as it is), a new one is added on top (first page, default order). The
// pill in the top bar counts the new rows not seen yet, the selection and an open drawer are not touched.
(function () {
  "use strict";

  // root: the page, or the rows of the list loaded after it (console/_rows.html)
  function setupLiveUpdates(root) {
    const table = root.querySelector("table[data-live-updates]");
    const pill = document.querySelector("[data-live-pill]");
    // the grey rows while the list loads: the real table comes with its rows
    if (!table || !pill || table.dataset.liveReady || table.closest(".rows-loading")) return;
    table.dataset.liveReady = "1";
    const tbody = table.tBodies[0];
    const baseTitle = document.title;
    const unseen = new Set();
    let elsewhere = 0;
    let after = table.dataset.after;
    const seen = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        unseen.delete(entry.target);
        seen.unobserve(entry.target);
      });
      renderPill();
    }, {
      // behind the sticky top bar is not seen, and most of the row must be visible
      rootMargin: `-${document.querySelector(".topbar")?.offsetHeight || 0}px 0px 0px 0px`, threshold: 0.6,
    });

    function renderPill() {
      const count = unseen.size + elsewhere;
      pill.hidden = !count;
      pill.querySelector("[data-live-pill-count]").textContent = count;
      pill.querySelector("[data-live-pill-text]").textContent =
        ` ${count === 1 ? pill.dataset.one : pill.dataset.many}${unseen.size ? "" : ` ${pill.dataset.elsewhere}`}`;
      document.title = count ? `(${count}) ${baseTitle}` : baseTitle;
    }

    function apply(rows) {
      rows.forEach((row) => {
        const current = [...tbody.rows].find((other) => other.dataset.key === row.dataset.key);
        const box = current?.querySelector('td.select input[type="checkbox"]');
        if (box?.checked) row.querySelector('td.select input[type="checkbox"]').checked = true;
        if (current) {
          // the new row takes its place in the count, the old one is gone
          unseen.delete(current);
          seen.unobserve(current);
        }
        if (current && !("insert" in table.dataset)) {
          current.replaceWith(row);
        } else if ("insert" in table.dataset) {
          current?.remove();
          tbody.querySelector("tr.empty")?.remove();
          tbody.prepend(row);
        } else {
          elsewhere += 1;
          return;
        }
        row.classList.add("new");
        unseen.add(row);
        seen.observe(row);
      });
      htmx.process(tbody);
      SantaConsole.table.labelTables(table.closest(".table-scroll") || table);
      SantaConsole.table.setupRowFocus(document);
      SantaConsole.table.updateAllSelections();
      renderPill();
    }

    async function poll() {
      // the connection check of core.js takes over while the server is gone
      if (document.hidden || SantaConsole.connection?.isLost()) return;
      const url = new URL(table.dataset.liveUpdates, window.location.href);
      url.searchParams.set("after", after);
      try {
        // never hanging: a poll without an answer would hold one of the browser's few connections
        const response = await fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin",
                                            signal: AbortSignal.timeout(SantaConsole.connection?.timeout() || 6000) });
        if (response.status !== 200) return;
        const wrapper = document.createElement("template");
        wrapper.innerHTML = await response.text();
        const update = wrapper.content.querySelector("template[data-max-pk]");
        if (!update) return;
        after = update.dataset.maxPk;
        // newest last, so that prepending keeps the newest on top
        apply([...update.content.querySelectorAll("tr[data-key]")].reverse());
      } catch (error) {
        // no answer in time: core.js checks the server; a network error: it is gone
        if (error.name === "TimeoutError") SantaConsole.connection?.suspect();
        else if (!document.hidden) SantaConsole.connection?.lost();
      }
    }

    pill.addEventListener("click", (event) => {
      const first = [...tbody.rows].find((row) => unseen.has(row));
      if (!first) return;
      event.preventDefault();
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      first.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    });
    // the page was switched (hx-boost): this table is gone, the new page sets up its own
    const timer = setInterval(() => {
      if (table.isConnected) poll();
      else clearInterval(timer);
    }, 10000);
  }

  SantaConsole.onInit(setupLiveUpdates);
})();
