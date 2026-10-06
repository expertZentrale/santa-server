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
})();
