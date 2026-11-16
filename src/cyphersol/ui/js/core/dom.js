/* DOM helpers: escaping, an auto-escaping `html` tagged template, event delegation, focus trapping.
 *
 * SECURITY CONTRACT: every server- or user-derived string reaches the DOM either via textContent or via
 * html`...` (which escapes every interpolation). Never build markup with string concatenation + innerHTML.
 * The page CSP forbids inline handlers and inline style attributes: use delegate()/addEventListener and
 * CSSOM (el.style.setProperty) instead of on*= / style="…" in templates.
 */

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" };

/** Escape a value for HTML text or a quoted attribute. null/undefined -> "". */
export const esc = (s) => String(s ?? "").replace(/[&<>"'`]/g, (c) => ESC_MAP[c]);

/** Marker class for already-safe markup produced by html`` or raw(). */
export class SafeHTML {
  constructor(str) {
    this.__html = String(str);
  }
  toString() {
    return this.__html;
  }
}

/** Mark a trusted, developer-authored string (NEVER server data) as safe markup. */
export const raw = (str) => new SafeHTML(str);

function interp(v) {
  if (v == null || v === false || v === true) return "";
  if (v instanceof SafeHTML) return v.__html;
  if (Array.isArray(v)) return v.map(interp).join("");
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  return esc(v);
}

/**
 * Auto-escaping tagged template. Interpolations are escaped unless they are SafeHTML (nested html``/raw()).
 * Arrays are flattened (so `${rows.map((r) => html`<li>${r.name}</li>`)}` works). null/undefined/booleans
 * render as "" so `${cond && html`...`}` is safe.
 */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i += 1) out += interp(values[i]) + strings[i + 1];
  return new SafeHTML(out);
}

/** Join SafeHTML fragments with an optional separator. */
export const join = (parts, sep = "") => new SafeHTML(parts.map(interp).join(interp(sep)));

/** Replace el's children with the given SafeHTML (strings are rendered as escaped text). Returns el. */
export function render(el, content) {
  if (!el) return el;
  if (content instanceof SafeHTML) el.innerHTML = content.__html;
  else if (content instanceof Node) el.replaceChildren(content);
  else el.textContent = content == null ? "" : String(content);
  return el;
}

/** Parse SafeHTML into a single Element (first element child) or DocumentFragment when multiple. */
export function toNode(content) {
  const t = document.createElement("template");
  t.innerHTML = content instanceof SafeHTML ? content.__html : esc(content);
  return t.content.childElementCount === 1 ? t.content.firstElementChild : t.content;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** addEventListener that returns an off() function. */
export function on(target, type, handler, opts) {
  target.addEventListener(type, handler, opts);
  return () => target.removeEventListener(type, handler, opts);
}

/**
 * Event delegation: handler(event, matchedElement) runs when `type` fires on an element matching `selector`
 * inside root. Returns off(). Typical use with data-action attributes:
 *   delegate(el, "click", "[data-action]", (e, btn) => actions[btn.dataset.action]?.(btn));
 */
export function delegate(root, type, selector, handler, opts) {
  const listener = (event) => {
    const start = event.target instanceof Element ? event.target : event.target?.parentElement;
    const match = start?.closest(selector);
    if (match && root.contains(match)) handler(event, match);
  };
  root.addEventListener(type, listener, opts);
  return () => root.removeEventListener(type, listener, opts);
}

export const FOCUSABLE =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), iframe, [tabindex]:not([tabindex="-1"]), [contenteditable="true"]';

export const focusables = (root) =>
  $$(FOCUSABLE, root).filter((el) => !el.hidden && el.getClientRects().length > 0 && !el.closest("[inert]"));

/**
 * Keep Tab focus inside container until release() is called. Restores focus to the previously focused
 * element on release (pass { restore: false } to skip). Native modal <dialog> already traps focus; use this
 * for custom popovers/drawers.
 */
export function trapFocus(container, { initial = null, restore = true } = {}) {
  const previous = document.activeElement;
  const onKey = (e) => {
    if (e.key !== "Tab") return;
    const items = focusables(container);
    if (!items.length) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  container.addEventListener("keydown", onKey);
  const target = typeof initial === "string" ? $(initial, container) : initial;
  (target || focusables(container)[0] || container).focus?.();
  return () => {
    container.removeEventListener("keydown", onKey);
    if (restore && previous && typeof previous.focus === "function" && document.contains(previous)) previous.focus();
  };
}

export function debounce(fn, ms = 250) {
  let t = null;
  const d = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  d.cancel = () => clearTimeout(t);
  d.flush = (...args) => {
    clearTimeout(t);
    fn(...args);
  };
  return d;
}

/**
 * Politely announce a one-line status to screen readers (shared sr-only role="status" region #sr-announcer).
 * Use instead of aria-live on large containers: announce("Eligibility recalculated: max EMI ₹X").
 */
export function announce(message) {
  let r = document.getElementById("sr-announcer");
  if (!r) {
    r = document.createElement("div");
    r.id = "sr-announcer";
    r.className = "sr-only";
    r.setAttribute("role", "status");
    r.setAttribute("aria-live", "polite");
    r.setAttribute("aria-atomic", "true");
    document.body.appendChild(r);
  }
  r.textContent = "";
  const text = String(message || "");
  if (text) setTimeout(() => (r.textContent = text), 60); // clear-then-set so a repeated message is re-read
}

let uidSeq = 0;
/** Unique DOM id ("prefix-12"). */
export const uid = (prefix = "id") => `${prefix}-${(uidSeq += 1)}`;

/** Read a CSS custom property (token) from :root, e.g. cssVar("--chart-1"). For canvas renderers. */
export const cssVar = (name, el = document.documentElement) => getComputedStyle(el).getPropertyValue(name).trim();

/** Set CSS custom properties via CSSOM (CSP-safe replacement for style="--x:…"). */
export function setVars(el, vars) {
  for (const [k, v] of Object.entries(vars)) el.style.setProperty(k.startsWith("--") ? k : `--${k}`, String(v));
  return el;
}

/** Read <form> controls into a plain object (checkbox -> boolean, number -> Number|null, multiple -> array). */
export function formValues(form) {
  const out = {};
  for (const el of Array.from(form.elements)) {
    if (!el.name || el.disabled) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "radio") {
      if (el.checked) out[el.name] = el.value;
    } else if (el.type === "number" || el.type === "range") out[el.name] = el.value === "" ? null : Number(el.value);
    else if (el.multiple) out[el.name] = Array.from(el.selectedOptions).map((o) => o.value);
    else out[el.name] = el.value;
  }
  return out;
}
