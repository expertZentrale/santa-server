// New events without a reload: every 10 s the changed rows come from the server. A row that is already in the
// list is replaced (its checkbox stays as it is), a new one is added on top (first page, default order). The
// pill in the top bar counts the new rows not seen yet, the selection and an open drawer are not touched.
(function () {
  "use strict";

  function setupLiveUpdates() {
    const table = document.querySelector("table[data-live-updates]");
    const pill = document.querySelector("[data-live-pill]");
    if (!table || !pill) return;
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
      if (document.hidden) return;
      const url = new URL(table.dataset.liveUpdates, window.location.href);
      url.searchParams.set("after", after);
      try {
        const response = await fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" });
        if (response.status !== 200) return;
        const wrapper = document.createElement("template");
        wrapper.innerHTML = await response.text();
        const update = wrapper.content.querySelector("template[data-max-pk]");
        if (!update) return;
        after = update.dataset.maxPk;
        // newest last, so that prepending keeps the newest on top
        apply([...update.content.querySelectorAll("tr[data-key]")].reverse());
      } catch (error) {
        // offline for a moment: the next poll tries again
      }
    }

    pill.addEventListener("click", (event) => {
      const first = [...tbody.rows].find((row) => unseen.has(row));
      if (!first) return;
      event.preventDefault();
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      first.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    });
    setInterval(poll, 10000);
  }

  document.addEventListener("DOMContentLoaded", setupLiveUpdates);
})();
