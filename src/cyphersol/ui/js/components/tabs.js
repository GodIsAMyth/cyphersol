/* WAI-ARIA tabs (automatic activation, roving tabindex, ←/→/Home/End).
 *
 * const tabs = createTabs(container, {
 *   label: "Draw tool sections",                        // aria-label for the tablist
 *   tabs: [{ id: "upload", label: "Upload", badge: 3, icon: "upload" }, { id: "draw", label: "Draw tool" }],
 *   selected: ctx.params.tab || "upload",
 *   onChange: (id, panelEl) => ctx.setParams({ tab: id }),  // also fires once for the initial tab when fireInitial
 *   fireInitial: true,
 * });
 * tabs.panel("upload")  -> the tabpanel element to render into
 * tabs.select("draw")   -> programmatic select (fires onChange)
 * tabs.setBadge("upload", 5)
 */
import { html, render } from "../core/dom.js";
import { icon } from "./icons.js";

let seq = 0;

export function createTabs(container, { tabs, selected = null, onChange = null, label = "Sections", fireInitial = true } = {}) {
  seq += 1;
  const base = `tabs${seq}`;
  const first = tabs.find((t) => t.id === selected) ? selected : tabs[0]?.id;
  render(
    container,
    html`<div class="tabs">
      <div class="tablist" role="tablist" aria-label="${label}">
        ${tabs.map(
          (t) => html`<button type="button" class="tab" role="tab" id="${base}-tab-${t.id}" aria-controls="${base}-panel-${t.id}"
              aria-selected="${t.id === first ? "true" : "false"}" tabindex="${t.id === first ? "0" : "-1"}" data-tab="${t.id}">
              ${t.icon ? icon(t.icon, { size: "sm" }) : ""}<span>${t.label}</span>
              <span class="badge badge--neutral" data-tab-badge ${t.badge == null || t.badge === "" ? "hidden" : ""}>${t.badge ?? ""}</span>
            </button>`
        )}
      </div>
      ${tabs.map(
        (t) => html`<div class="tabpanel" role="tabpanel" id="${base}-panel-${t.id}" aria-labelledby="${base}-tab-${t.id}"
            tabindex="0" data-panel="${t.id}" ${t.id === first ? "" : "hidden"}></div>`
      )}
    </div>`
  );
  const tablist = container.querySelector('[role="tablist"]');
  const tabEls = () => Array.from(tablist.querySelectorAll('[role="tab"]'));
  let current = first;

  function select(id, { focus = false, silent = false } = {}) {
    const tabEl = tablist.querySelector(`[data-tab="${CSS.escape(id)}"]`);
    if (!tabEl) return;
    for (const t of tabEls()) {
      const on = t === tabEl;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    }
    container.querySelectorAll('[role="tabpanel"]').forEach((p) => {
      p.hidden = p.dataset.panel !== id;
    });
    if (focus) tabEl.focus();
    const changed = current !== id;
    current = id;
    if (!silent && changed) onChange?.(id, panel(id));
  }

  function panel(id) {
    return container.querySelector(`[data-panel="${CSS.escape(id)}"]`);
  }

  tablist.addEventListener("click", (e) => {
    const t = e.target.closest('[role="tab"]');
    if (t) select(t.dataset.tab);
  });
  tablist.addEventListener("keydown", (e) => {
    const list = tabEls();
    const i = list.indexOf(document.activeElement);
    if (i < 0) return;
    let next = null;
    if (e.key === "ArrowRight") next = list[(i + 1) % list.length];
    else if (e.key === "ArrowLeft") next = list[(i - 1 + list.length) % list.length];
    else if (e.key === "Home") next = list[0];
    else if (e.key === "End") next = list[list.length - 1];
    if (next) {
      e.preventDefault();
      select(next.dataset.tab, { focus: true });
    }
  });

  if (fireInitial && first) onChange?.(first, panel(first));

  return {
    el: container,
    select,
    panel,
    get selected() {
      return current;
    },
    setBadge(id, value) {
      const b = tablist.querySelector(`[data-tab="${CSS.escape(id)}"] [data-tab-badge]`);
      if (!b) return;
      b.hidden = value == null || value === "";
      b.textContent = value == null ? "" : String(value);
    },
    setLabel(id, text) {
      const s = tablist.querySelector(`[data-tab="${CSS.escape(id)}"] span:not([data-tab-badge])`);
      if (s) s.textContent = text;
    },
  };
}

