/* KPI / stat tiles.
 *
 * kpiGrid([
 *   { label: "Total credits", value: inrCompact(s.total_credits), title: inr(s.total_credits), sub: "inflow", icon: "arrow-down-left" },
 *   { label: "Suspense", value: int(s.suspense_count), tone: s.suspense_count ? "warn" : "success", href: ctx.href("suspense") },
 * ], { compact: false, label: "Case summary" })  -> SafeHTML
 * tone: "neutral" | "success" | "danger" | "warn" | "critical" | "accent". `title` = exact value tooltip.
 * Values are rendered as text (escaped); pass already-formatted strings.
 */
import { html } from "../core/dom.js";
import { icon } from "./icons.js";

export function kpiTile({ label, value, sub = "", tone = "", title = "", icon: ic = "", href = "" }) {
  const inner = html`<span class="kpi__label">${ic ? icon(ic, { size: "sm" }) : ""}${label}</span>
    <span class="kpi__value tabular" ${title ? html`title="${title}"` : ""}>${value}</span>
    ${sub ? html`<span class="kpi__sub">${sub}</span>` : ""}`;
  return href
    ? html`<a class="kpi card--interactive" href="${href}" ${tone ? html`data-tone="${tone}"` : ""}>${inner}</a>`
    : html`<div class="kpi" ${tone ? html`data-tone="${tone}"` : ""}>${inner}</div>`;
}

export function kpiGrid(items, { compact = false, label = "" } = {}) {
  return html`<div class="kpi-grid ${compact ? "kpi-grid--compact" : ""}" role="list" ${label ? html`aria-label="${label}"` : ""}>
    ${items.filter(Boolean).map((it) => html`<div role="listitem" class="min-w-0">${kpiTile(it)}</div>`)}
  </div>`;
}
