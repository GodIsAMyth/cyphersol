/* ⌘K / Ctrl+K command palette over the commands registry (core/commands.js).
 * initPalette() binds the shortcut once; openPalette() opens it programmatically.
 * ARIA: combobox input controlling a listbox; ↑/↓ move, Enter runs, Esc closes. */
import { getCommands, searchCommands } from "../core/commands.js";
import { html, render } from "../core/dom.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";

let dlg = null;

export function initPalette() {
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (dlg) closePalette();
      else openPalette();
    }
  });
}

export function closePalette() {
  if (dlg && dlg.open) dlg.close();
}

export function openPalette(initialQuery = "") {
  if (dlg) return;
  dlg = document.createElement("dialog");
  dlg.className = "palette";
  dlg.setAttribute("aria-label", "Command palette");
  render(
    dlg,
    html`<div class="palette__search">
        ${icon("search")}
        <input class="palette__input" type="text" role="combobox" aria-expanded="true" aria-controls="palette-list"
          aria-autocomplete="list" aria-label="Type a command or search" placeholder="Type a command or search…"
          autocomplete="off" spellcheck="false" />
        <kbd>Esc</kbd>
      </div>
      <ul class="palette__list" id="palette-list" role="listbox" aria-label="Commands"></ul>
      <div class="palette__foot"><span><kbd>↑</kbd> <kbd>↓</kbd> navigate</span><span><kbd>↵</kbd> run</span><span><kbd>Esc</kbd> close</span></div>`
  );
  const input = dlg.querySelector("input");
  const list = dlg.querySelector("ul");
  let results = [];
  let active = 0;

  function draw() {
    results = searchCommands(input.value, getCommands()).slice(0, 60);
    active = Math.min(active, Math.max(0, results.length - 1));
    if (!results.length) {
      render(list, html`<li class="palette__empty" role="presentation">No matching commands</li>`);
      input.removeAttribute("aria-activedescendant");
      return;
    }
    const grouped = !input.value.trim();
    let lastGroup = null;
    render(
      list,
      html`${results.map((c, i) => {
        const header = grouped && c.group !== lastGroup ? html`<li class="palette__group caps" role="presentation">${c.group}</li>` : "";
        lastGroup = c.group;
        return html`${header}<li class="palette__item" role="option" id="palette-opt-${i}" data-index="${i}"
            aria-selected="${i === active ? "true" : "false"}">
            ${icon(c.icon || "arrow-right")}<span class="truncate">${c.title}</span>
            <span class="palette__item-hint">${c.hint || (grouped ? "" : c.group)}</span></li>`;
      })}`
    );
    input.setAttribute("aria-activedescendant", `palette-opt-${active}`);
  }

  function move(delta) {
    if (!results.length) return;
    active = (active + delta + results.length) % results.length;
    list.querySelectorAll('[role="option"]').forEach((o) => o.setAttribute("aria-selected", o.dataset.index === String(active) ? "true" : "false"));
    input.setAttribute("aria-activedescendant", `palette-opt-${active}`);
    list.querySelector(`#palette-opt-${active}`)?.scrollIntoView({ block: "nearest" });
  }

  async function runAt(i) {
    const cmd = results[i];
    if (!cmd) return;
    closePalette();
    try {
      await cmd.run();
    } catch (err) {
      toast.error(err);
    }
  }

  input.addEventListener("input", () => {
    active = 0;
    draw();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      runAt(active);
    }
  });
  list.addEventListener("click", (e) => {
    const li = e.target.closest('[role="option"]');
    if (li) runAt(Number(li.dataset.index));
  });
  list.addEventListener("pointermove", (e) => {
    const li = e.target.closest('[role="option"]');
    if (li && Number(li.dataset.index) !== active) move(Number(li.dataset.index) - active);
  });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) closePalette();
  });
  dlg.addEventListener("close", () => {
    dlg.remove();
    dlg = null;
  });
  document.body.appendChild(dlg);
  dlg.showModal();
  input.value = initialQuery;
  draw();
  input.focus();
}
