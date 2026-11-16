/* Side drawer (inspector / evidence panel) on a modal <dialog>: focus trapped, Esc + backdrop close.
 *
 * const d = openDrawer({ title: "Evidence", body: html`…`, width: 560, side: "right", footer: html`…`,
 *                        onClose: () => {} });
 * d.setTitle("…"); d.setBody(html`…`); d.body (element, for delegation); d.close(); await d.closed;
 * Only one drawer is open at a time; opening another closes the previous one.
 */
import { html, render, setVars } from "../core/dom.js";
import { icon } from "./icons.js";

let active = null;

export function openDrawer({ title = "", body = "", footer = null, width = 520, side = "right", onClose = null, className = "" } = {}) {
  if (active) active.close();
  const dlg = document.createElement("dialog");
  dlg.className = `drawer ${side === "left" ? "drawer--left" : ""} ${className}`.trim();
  setVars(dlg, { "--drawer-w": `${width}px` });
  const titleId = `drawer-title-${Math.random().toString(36).slice(2, 8)}`;
  dlg.setAttribute("aria-labelledby", titleId);
  render(
    dlg,
    html`<div class="drawer__head">
        <h2 class="drawer__title truncate" id="${titleId}"></h2>
        <button type="button" class="btn btn--ghost btn--icon" data-drawer-close aria-label="Close panel">${icon("x")}</button>
      </div>
      <div class="drawer__body"></div>
      <div class="drawer__foot" hidden></div>`
  );
  const titleEl = dlg.querySelector(".drawer__title");
  const bodyEl = dlg.querySelector(".drawer__body");
  const footEl = dlg.querySelector(".drawer__foot");
  const setTitle = (t) => render(titleEl, t);
  const setBody = (b) => (b instanceof Node ? bodyEl.replaceChildren(b) : render(bodyEl, b));
  const setFooter = (f) => {
    footEl.hidden = !f;
    if (f) f instanceof Node ? footEl.replaceChildren(f) : render(footEl, f);
  };
  setTitle(title);
  setBody(body);
  setFooter(footer);

  let resolve;
  const closed = new Promise((r) => {
    resolve = r;
  });
  const close = () => {
    if (dlg.open) dlg.close();
  };
  dlg.addEventListener("click", (e) => {
    if (e.target.closest("[data-drawer-close]") || e.target === dlg) close();
  });
  dlg.addEventListener("close", () => {
    dlg.remove();
    if (active && active.el === dlg) active = null;
    try {
      onClose?.();
    } finally {
      resolve();
    }
  });
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.querySelector("[data-drawer-close]").focus();
  const api = { el: dlg, body: bodyEl, footer: footEl, setTitle, setBody, setFooter, close, closed };
  active = api;
  return api;
}

export const closeDrawer = () => active?.close();
