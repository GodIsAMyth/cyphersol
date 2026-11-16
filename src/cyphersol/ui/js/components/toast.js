/* Toast notifications (replace alert()) and the withBusy() async-action wrapper.
 *
 * toast("Saved")                                   // info
 * toast.success("Rule saved", { title: "Suspense" })
 * toast.error(err)                                 // ApiError/Error -> friendly text (+ error id); aborts ignored
 * toast("Merged 3 aliases", { action: { label: "Undo", run: () => … } })   // persists until dismissed
 * Returns dismiss(). Region: #toast-region (aria-live polite; errors are role="alert").
 */
import { html, render } from "../core/dom.js";
import { isAbort } from "../core/api.js";
import { icon } from "./icons.js";

const ICONS = { info: "info", success: "check-circle", warn: "alert", error: "x-circle" };
const DEFAULT_TIMEOUT = { info: 4500, success: 4000, warn: 7000, error: 9000 };

/** The topmost open modal dialog, if any (everything outside it is inert while it is open). */
function topModal() {
  let list;
  try {
    list = document.querySelectorAll("dialog:modal");
  } catch (_e) {
    list = document.querySelectorAll("dialog[open]");
  }
  return list.length ? list[list.length - 1] : null;
}

function region() {
  // While a modal <dialog> is open the page-level region is inert (not focusable, hidden from AT), so
  // toasts raised then go into a region inside that dialog instead.
  const dlg = topModal();
  if (dlg) {
    let inner = dlg.querySelector(":scope > .toast-region");
    if (!inner) {
      inner = document.createElement("div");
      inner.className = "toast-region toast-region--dialog";
      inner.setAttribute("aria-live", "polite");
      inner.setAttribute("aria-relevant", "additions");
      dlg.appendChild(inner);
    }
    return inner;
  }
  let r = document.getElementById("toast-region");
  if (!r) {
    r = document.createElement("div");
    r.id = "toast-region";
    r.className = "toast-region";
    r.setAttribute("aria-live", "polite");
    document.body.appendChild(r);
  }
  return r;
}

export function toast(message, { type = "info", title = "", timeout, action = null } = {}) {
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.type = type;
  if (type === "error") el.setAttribute("role", "alert");
  render(
    el,
    html`<span class="toast__icon">${icon(ICONS[type] || "info")}</span>
      <div class="toast__body">
        ${title ? html`<div class="toast__title">${title}</div>` : ""}
        <div class="${title ? "toast__msg" : ""}">${message}</div>
        ${action ? html`<div class="toast__actions"><button type="button" class="btn btn--sm" data-toast-action>${action.label}</button></div>` : ""}
      </div>
      <button type="button" class="btn btn--ghost btn--icon btn--sm" data-toast-close aria-label="Dismiss notification">${icon("x", { size: "sm" })}</button>`
  );
  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    if (!el.isConnected) return;
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector("[data-toast-close]").addEventListener("click", dismiss);
  if (action) {
    el.querySelector("[data-toast-action]").addEventListener("click", async () => {
      dismiss();
      try {
        await action.run();
      } catch (err) {
        toast.error(err);
      }
    });
  }
  // a toast that carries an action (Undo, Open settings…) stays until dismissed so keyboard and screen-reader
  // users can reach it (WCAG 2.2.1); pass an explicit timeout to override.
  const ms = timeout === undefined ? (action ? 0 : DEFAULT_TIMEOUT[type] || 5000) : timeout;
  const arm = () => {
    if (ms > 0) timer = setTimeout(dismiss, ms);
  };
  el.addEventListener("mouseenter", () => clearTimeout(timer));
  el.addEventListener("mouseleave", arm);
  el.addEventListener("focusin", () => clearTimeout(timer));
  region().appendChild(el);
  arm();
  return dismiss;
}

toast.info = (msg, opts) => toast(msg, { ...opts, type: "info" });
toast.success = (msg, opts) => toast(msg, { ...opts, type: "success" });
toast.warn = (msg, opts) => toast(msg, { ...opts, type: "warn" });
/** Error toast from an Error/ApiError/string. AbortErrors are silently ignored. */
toast.error = (err, opts = {}) => {
  if (isAbort(err)) return () => {};
  const msg = typeof err === "string" ? err : err?.message || "Something went wrong.";
  const id = err && err.errorId ? ` (ref ${err.errorId})` : "";
  return toast(msg + id, { title: opts.title || "Action failed", ...opts, type: "error" });
};

/**
 * Run an async action with busy state + error toast, preventing double submits (UI-S-04/05).
 *   btn.addEventListener("click", () => withBusy(btn, () => ctx.api.post(...), { success: "Saved" }));
 * - el: button/element (or null); gets aria-busy + disabled while running; re-entry is ignored.
 * - opts.success: string | (result) => string  -> success toast
 * - opts.error: title for the error toast (default "Action failed"); opts.silent: no error toast
 * Resolves to the action's result, or undefined when it failed / was ignored.
 */
export async function withBusy(el, fn, { success, error, silent = false } = {}) {
  if (el && el.dataset.busy === "1") return undefined;
  if (el) {
    el.dataset.busy = "1";
    el.setAttribute("aria-busy", "true");
    if ("disabled" in el) el.disabled = true;
  }
  try {
    const result = await fn();
    if (success) toast.success(typeof success === "function" ? success(result) : success);
    return result;
  } catch (err) {
    if (!silent) toast.error(err, error ? { title: error } : {});
    else if (!isAbort(err)) console.error(err);
    return undefined;
  } finally {
    if (el) {
      delete el.dataset.busy;
      el.removeAttribute("aria-busy");
      if ("disabled" in el) el.disabled = false;
    }
  }
}
