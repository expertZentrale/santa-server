// File uploads as drop zones (forms.FileDropInput): the input covers the zone, so the browser takes the dropped
// file itself; this only marks the zone while dragging and shows the chosen file
(function () {
  "use strict";

  function formatSize(bytes) {
    const lang = document.documentElement.lang || undefined;
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024)).toLocaleString(lang)} KB`;
    return `${(bytes / 1024 / 1024).toLocaleString(lang, { maximumFractionDigits: 1 })} MB`;
  }

  function setupDrop(zone) {
    if (zone.dataset.fileDropReady) return;
    zone.dataset.fileDropReady = "1";
    const input = zone.querySelector("input[type=file]");
    const output = zone.querySelector("[data-file-name]");
    const show = () => {
      const file = input.files[0];
      output.textContent = file ? `${file.name} · ${formatSize(file.size)}` : "";
      zone.classList.toggle("has-file", Boolean(file));
    };
    input.addEventListener("change", show);
    input.addEventListener("dragenter", () => zone.classList.add("dragging"));
    ["dragleave", "drop"].forEach((type) => input.addEventListener(type, () => zone.classList.remove("dragging")));
    // e.g. Back to a page whose form kept the file
    show();
  }

  SantaConsole.onInit((root) => root.querySelectorAll("[data-file-drop]").forEach(setupDrop));

  // A form with a file that comes back from the server (a field missing, e.g. no group): it gets the file again.
  // The server can't fill a file input, and it keeps nothing of an upload, so the browser does.
  let sent = null;
  document.addEventListener("htmx:beforeRequest", (event) => {
    const elt = event.detail.elt;
    const form = elt?.tagName === "FORM" ? elt : elt?.closest?.("form");
    const files = [...(form?.querySelectorAll("input[type=file]") || [])].filter((input) => input.files.length)
      .map((input) => ({ name: input.name, files: [...input.files] }));
    sent = files.length ? { config: event.detail.requestConfig, files } : null;
  });
  // after core.js has set up the new content (its drop zones listen for "change")
  document.addEventListener("htmx:afterSwap", (event) => {
    if (!sent || event.detail.requestConfig !== sent.config || !event.detail.target || !window.DataTransfer) return;
    const { files } = sent;
    sent = null;
    files.forEach(({ name, files: chosen }) => {
      const input = event.detail.target.querySelector(`input[type=file][name="${CSS.escape(name)}"]`);
      if (!input || input.files.length) return;
      const transfer = new DataTransfer();
      chosen.forEach((file) => transfer.items.add(file));
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
})();
