/* Unknown route. */
import { html, render } from "../core/dom.js";
import { emptyState } from "../components/states.js";

export function mount(el, ctx) {
  render(
    el,
    html`<div class="card">${emptyState({
      icon: "alert",
      title: "Page not found",
      message: "This address does not match any CypherSOL view.",
      action: { label: ctx.caseId ? "Go to case overview" : "Go to cases", href: ctx.caseId ? ctx.href("overview") : "#/cases" },
    })}</div>`
  );
}
