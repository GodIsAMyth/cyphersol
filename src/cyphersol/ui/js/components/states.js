/* Empty / error / loading states and the page head. All return SafeHTML.
 *
 * emptyState({ icon: "inbox", title: "No suspense items", message: "…", action: { label: "Upload", href } | { label, id } , tone: "success" })
 * errorState(err, { retryId: "retry" })   // button gets data-action="<retryId>" for delegation
 * skeleton({ kind: "table" | "cards" | "kpis" | "text" | "block", rows: 6 })
 * spinner()
 */
import { html } from "../core/dom.js";
import { icon } from "./icons.js";

export function emptyState({ icon: ic = "inbox", title = "Nothing here yet", message = "", action = null, tone = "", compact = false } = {}) {
  return html`<div class="empty ${compact ? "empty--compact" : ""} ${tone ? `empty--${tone}` : ""}">
    <div class="empty__icon">${icon(ic, { size: "lg" })}</div>
    <div class="empty__title">${title}</div>
    ${message ? html`<p class="empty__msg">${message}</p>` : ""}
    ${action
      ? action.href
        ? html`<a class="btn btn--primary" href="${action.href}">${action.label}</a>`
        : html`<button type="button" class="btn btn--primary" data-action="${action.id || "empty-action"}">${action.label}</button>`
      : ""}
  </div>`;
}

export function errorState(err, { retryId = "retry", title = "Could not load this view", compact = false } = {}) {
  const msg = err?.message || String(err || "Unknown error");
  const ref = err?.errorId ? ` (ref ${err.errorId})` : "";
  return html`<div class="empty empty--error ${compact ? "empty--compact" : ""}" role="alert">
    <div class="empty__icon">${icon("alert", { size: "lg" })}</div>
    <div class="empty__title">${title}</div>
    <p class="empty__msg">${msg}${ref}</p>
    ${retryId ? html`<button type="button" class="btn" data-action="${retryId}">${icon("refresh", { size: "sm" })}Try again</button>` : ""}
  </div>`;
}

export function skeleton({ kind = "text", rows = 6 } = {}) {
  const n = Array.from({ length: rows });
  if (kind === "kpis") return html`<div class="kpi-grid" aria-hidden="true">${n.map(() => html`<div class="skeleton skeleton--kpi"></div>`)}</div>`;
  if (kind === "block") return html`<div class="skeleton skeleton--block" aria-hidden="true"></div>`;
  if (kind === "cards") return html`<div class="grid-3" aria-hidden="true">${n.map(() => html`<div class="skeleton skeleton--block"></div>`)}</div>`;
  if (kind === "table") {
    return html`<div class="card" aria-hidden="true"><div class="card__body skeleton-stack">
      <div class="skeleton skeleton--title"></div>${n.map(() => html`<div class="skeleton skeleton--line"></div>`)}</div></div>`;
  }
  return html`<div class="skeleton-stack" aria-hidden="true">${n.map(() => html`<div class="skeleton skeleton--line"></div>`)}</div>`;
}

/** Generic page skeleton used by the router while a view loads. */
export const viewSkeleton = () => html`<div class="stack" aria-busy="true" aria-label="Loading">
  <div class="skeleton skeleton--title"></div>${skeleton({ kind: "kpis", rows: 4 })}${skeleton({ kind: "table", rows: 8 })}</div>`;

export const spinner = (label = "Loading") => html`<span class="spinner" role="status" aria-label="${label}"></span>`;

/** Standard page header used by every view. actions: SafeHTML for the right side. */
export function pageHead({ title, sub = "", actions = "" }) {
  return html`<header class="page-head">
    <div class="page-head__text"><h1 class="page-title" tabindex="-1">${title}</h1>${sub ? html`<p class="page-sub">${sub}</p>` : ""}</div>
    ${actions ? html`<div class="page-actions">${actions}</div>` : ""}
  </header>`;
}
