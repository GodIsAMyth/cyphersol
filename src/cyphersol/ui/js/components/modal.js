/* Accessible modal dialogs on native <dialog> (focus is trapped by showModal(); Esc closes; focus returns
 * to the opener).
 *
 * const m = openModal({
 *   title: "Create case", description: "…", size: "md" | "sm" | "lg" | "xl",
 *   body: html`<div class="field">…<input name="title" required class="input"></div>`,   // SafeHTML | Node
 *   actions: [{ label: "Cancel", value: "cancel" }, { label: "Create", variant: "primary", type: "submit" }],
 *   onSubmit: async (values, { el, close }) => { …; return false to keep open },     // values = formValues()
 *   onOpen: (el) => {}, initialFocus: "[name=title]", dismissible: true,
 * });
 * const result = await m.closed;   // submit -> onSubmit's return value (or values); action -> its value; Esc -> null
 *
 * await confirmDialog({ title, message, confirmLabel: "Delete", danger: true }) -> boolean
 */
import { formValues, html, render } from "../core/dom.js";
import { icon } from "./icons.js";
import { isAbort } from "../core/api.js";

export function openModal({
  title,
  description = "",
  body = "",
  size = "md",
  actions = [{ label: "Close", value: "close" }],
  onSubmit = null,
  onOpen = null,
  initialFocus = null,
  dismissible = true,
  className = "",
} = {}) {
  const dlg = document.createElement("dialog");
  dlg.className = `modal modal--${size} ${className}`.trim();
  const titleId = `modal-title-${Math.random().toString(36).slice(2, 8)}`;
  dlg.setAttribute("aria-labelledby", titleId);
  render(
    dlg,
    html`<form class="modal__form" method="dialog" novalidate>
      <div class="modal__head">
        <div class="min-w-0">
          <h2 class="modal__title" id="${titleId}">${title}</h2>
          ${description ? html`<p class="modal__desc">${description}</p>` : ""}
        </div>
        ${dismissible ? html`<button type="button" class="btn btn--ghost btn--icon modal__close" data-modal-value="" aria-label="Close dialog">${icon("x")}</button>` : ""}
      </div>
      <div class="modal__error callout callout--danger" role="alert" hidden></div>
      <div class="modal__body"></div>
      ${actions && actions.length
        ? html`<div class="modal__foot">
            ${actions.map((a) =>
              a.spacer
                ? html`<span class="spacer"></span>`
                : html`<button type="${a.type === "submit" ? "submit" : "button"}" class="btn ${a.variant ? `btn--${a.variant}` : ""}"
                    ${a.type === "submit" ? "" : html`data-modal-value="${a.value ?? ""}"`} ${a.id ? html`id="${a.id}"` : ""}>${a.label}</button>`
            )}
          </div>`
        : ""}
    </form>`
  );
  const bodyEl = dlg.querySelector(".modal__body");
  const errorSlot = dlg.querySelector(".modal__error");
  if (body instanceof Node) bodyEl.replaceChildren(body);
  else render(bodyEl, body);
  const form = dlg.querySelector("form");

  let resolve;
  const closed = new Promise((r) => {
    resolve = r;
  });
  let result = null;
  const close = (value = null) => {
    result = value;
    if (dlg.open) dlg.close();
  };

  dlg.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-modal-value]");
    if (btn) {
      e.preventDefault();
      close(btn.dataset.modalValue || null);
    } else if (e.target === dlg && dismissible) {
      close(null); // backdrop click
    }
  });
  dlg.addEventListener("cancel", (e) => {
    if (!dismissible) e.preventDefault();
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    const submitBtn = form.querySelector('button[type="submit"]');
    const values = formValues(form);
    if (!onSubmit) {
      close(values);
      return;
    }
    if (submitBtn) {
      if (submitBtn.dataset.busy === "1") return;
      submitBtn.dataset.busy = "1";
      submitBtn.setAttribute("aria-busy", "true");
    }
    errorSlot.hidden = true;
    errorSlot.replaceChildren();
    try {
      const r = await onSubmit(values, { el: dlg, close, form });
      if (r !== false) close(r === undefined ? values : r);
    } catch (err) {
      if (isAbort(err)) return;
      // shown inside the dialog (the page-level toast region is inert while a modal is open)
      const msg = typeof err === "string" ? err : err?.message || "Something went wrong.";
      const ref = err && err.errorId ? ` (ref ${err.errorId})` : "";
      render(errorSlot, html`${icon("x-circle")}<div><strong>Could not complete this action.</strong> ${msg}${ref}</div>`);
      errorSlot.hidden = false;
      errorSlot.scrollIntoView?.({ block: "nearest" });
    } finally {
      if (submitBtn) {
        delete submitBtn.dataset.busy;
        submitBtn.removeAttribute("aria-busy");
      }
    }
  });
  dlg.addEventListener("close", () => {
    dlg.remove();
    resolve(result);
  });

  document.body.appendChild(dlg);
  dlg.showModal();
  const focusEl = initialFocus ? dlg.querySelector(initialFocus) : bodyEl.querySelector("input, select, textarea, button");
  (focusEl || dlg.querySelector(".modal__close") || dlg).focus();
  onOpen?.(dlg);
  return { el: dlg, body: bodyEl, form, close, closed };
}

/** Yes/no confirmation. Resolves true only when the confirm button is pressed. */
export async function confirmDialog({ title = "Are you sure?", message = "", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false } = {}) {
  const m = openModal({
    title,
    size: "sm",
    body: message ? html`<p class="text-2">${message}</p>` : "",
    actions: [
      { label: cancelLabel, value: "cancel" },
      { label: confirmLabel, value: "confirm", variant: danger ? "danger" : "primary" },
    ],
    initialFocus: '[data-modal-value="confirm"]',
  });
  return (await m.closed) === "confirm";
}
