// Feed page script: inlined into the <head> of index.html by scripts/webpage.py, so the theme is
// set before the first paint. Theme: the saved choice, else the browser's preference.
(() => {
  "use strict";

  const KEY = "theme";
  const root = document.documentElement;
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
  let choice = null;
  let toggle = null;

  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") {
      choice = saved;
    }
  } catch {
    // Storage disabled: follow the browser and keep a choice for this page view only.
  }

  function apply() {
    const theme = choice || (prefersDark.matches ? "dark" : "light");
    root.dataset.theme = theme;
    if (toggle) {
      const label = `Switch to ${theme === "dark" ? "light" : "dark"} theme`;
      toggle.setAttribute("aria-label", label);
      toggle.title = label;
    }
  }

  root.classList.add("js");
  apply();
  prefersDark.addEventListener("change", apply);

  function setupThemeToggle() {
    toggle = document.getElementById("theme-toggle");
    apply();
    toggle.addEventListener("click", () => {
      choice = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, choice);
      } catch {
        // Not persisted; still applies to this page view.
      }
      apply();
    });
  }

  function setupCopyButtons() {
    for (const button of document.querySelectorAll("[data-copy]")) {
      const code = document.getElementById(button.dataset.copy);
      const label = button.querySelector(".label");
      const icon = button.querySelector("use");
      let timer = 0;
      button.addEventListener("click", async () => {
        let copied = true;
        try {
          await navigator.clipboard.writeText(code.textContent);
        } catch {
          // No clipboard access (e.g. not https): select the text for a manual copy.
          copied = false;
          window.getSelection().selectAllChildren(code);
        }
        label.textContent = copied ? "Copied" : "Selected";
        icon.setAttribute("href", copied ? "#i-check" : "#i-copy");
        clearTimeout(timer);
        timer = setTimeout(() => {
          label.textContent = "Copy";
          icon.setAttribute("href", "#i-copy");
        }, 2000);
      });
    }
  }

  // Targets: either all are selected (every package is listed) or exactly one is (only its rows are
  // listed). Clicking a target selects only it; clicking the selected one, or "Show all", selects all.
  function setupTargetFilter() {
    const picker = document.getElementById("target-picker");
    const tiles = [...picker.querySelectorAll("[data-target]")];
    const bar = document.getElementById("target-filter");
    const showAll = document.getElementById("show-all");
    const sections = [...document.querySelectorAll("[data-filtered]")];
    let selected = null;

    function render() {
      for (const tile of tiles) {
        tile.setAttribute("aria-pressed", String(selected === null || tile.dataset.target === selected));
      }
      bar.hidden = selected === null;
      for (const name of document.querySelectorAll(".filter-name")) {
        name.textContent = selected || "";
      }
      for (const section of sections) {
        const groups = [...section.querySelectorAll(".pkg-group")];
        let shown = 0;
        for (const group of groups) {
          let rows = 0;
          for (const row of group.querySelectorAll("tbody tr")) {
            row.hidden = selected !== null && row.dataset.target !== selected;
            rows += row.hidden ? 0 : 1;
          }
          group.hidden = rows === 0;
          const count = group.querySelector(".count");
          if (count) {
            count.hidden = selected !== null;
          }
          shown += rows;
        }
        section.querySelector(".filter-tag").hidden = selected === null;
        // A section without any package says "None." already.
        section.querySelector(".filter-empty").hidden = shown > 0 || groups.length === 0;
      }
    }

    for (const tile of tiles) {
      tile.addEventListener("click", () => {
        selected = selected === tile.dataset.target ? null : tile.dataset.target;
        render();
      });
    }
    showAll.addEventListener("click", () => {
      const tile = tiles.find((t) => t.dataset.target === selected);
      selected = null;
      render();
      tile.focus(); // the button just hid itself
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupThemeToggle();
    setupCopyButtons();
    setupTargetFilter();
  });
})();
