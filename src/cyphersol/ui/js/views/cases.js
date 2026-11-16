/* Cases home (#/cases): every workspace in the local vault with its product badge and a live KPI strip
 * (statements, rows, credits / debits in compact ₹ L/Cr, suspense, flagged rows), search + mode filter,
 * "last opened" marker, and a create-case dialog with Edge / Trace workspace cards.
 *
 * Per-case KPIs come from the light overview payload (GET /api/cases/{id}/overview?tx_limit=0), fetched
 * lazily with bounded concurrency and cached for the lifetime of the view; the list itself is patched in
 * place (cards are keyed by case id) so a new case or a filter change never refetches the others. */
import { api, caseUrl, isAbort } from "../core/api.js";
import { CASE_MODES, STORAGE_KEYS, caseModeLabel, lsGet } from "../core/constants.js";
import { debounce, delegate, html, render } from "../core/dom.js";
import { date, inr, inrCompact, int, plural, relTime } from "../core/format.js";
import { primaryViews } from "../core/nav.js";
import { href } from "../core/router.js";
import { TOPICS } from "../core/store.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { openModal } from "../components/modal.js";
import { emptyState, pageHead } from "../components/states.js";
import { toast } from "../components/toast.js";

export const styles = ["views/cases.css"];

const MODE_FILTERS = [
  { value: "", label: "All" },
  { value: "cypher_edge", label: CASE_MODES.cypher_edge.label },
  { value: "cypher_trace", label: CASE_MODES.cypher_trace.label },
];
const SORTS = [
  { value: "recent", label: "Newest first" },
  { value: "title", label: "Title A–Z" },
];
const STAT_CONCURRENCY = 3;

const modeBadge = (mode) =>
  html`<span class="badge ${mode === "cypher_edge" ? "badge--info" : "badge--accent"}">${icon(mode === "cypher_edge" ? "receipt" : "network", { size: "sm" })}${caseModeLabel(mode)}</span>`;

/** Product-specific shortcut shown on each card (first analytic view after the shared ones). */
const SHORTCUTS = {
  cypher_edge: [{ view: "tally", label: "Tally" }, { view: "loan", label: "Loan" }],
  cypher_trace: [{ view: "network", label: "Network" }, { view: "mules", label: "Mules" }],
};

/**
 * Create-case dialog with Edge / Trace workspace cards. Exported so the shell can reuse it.
 * Resolves to the created case (or null when cancelled). onCreated(created) runs before the dialog closes.
 */
export function openCreateCaseDialog({ defaultMode = "cypher_trace", onCreated = null, store } = {}) {
  const m = openModal({
    title: "New case",
    description: "A case is a sealed workspace: statements, custody hashes, analysis and the audit trail stay together in the local vault.",
    size: "lg",
    body: html`<div class="stack v-cases-new">
      <div class="field">
        <label class="field__label" for="vc-new-title">Case / client title</label>
        <input class="input" id="vc-new-title" name="title" required maxlength="200" autocomplete="off"
          placeholder="e.g. EOW Case 109 or ABC Traders FY 2025-26" aria-describedby="vc-new-title-hint" />
        <span class="field__hint" id="vc-new-title-hint">Shown in the case switcher, exports and the forensic dossier.</span>
      </div>
      <fieldset class="v-cases-modes">
        <legend class="field__label">Workspace</legend>
        <div class="v-cases-mode-grid">
          ${Object.entries(CASE_MODES).map(
            ([value, mode]) => html`<label class="v-cases-mode">
              <input type="radio" name="mode" value="${value}" ${value === defaultMode ? html`checked` : ""} />
              <span class="v-cases-mode__body">
                <span class="v-cases-mode__title">${icon(value === "cypher_edge" ? "receipt" : "network")}${mode.label}</span>
                <span class="v-cases-mode__desc">${mode.desc}</span>
                <span class="v-cases-mode__views">${value === "cypher_edge"
                  ? "Tally XML · ITR compliance · Loan ABB / FOIR"
                  : "Network graph · Layering · Loops · Mule scoring · Profiles"}</span>
              </span>
            </label>`
          )}
        </div>
        <span class="field__hint">Both products share ingest, transactions, suspense and anomalies; the other product's views stay available under “More”.</span>
      </fieldset>
      <div class="field">
        <label class="field__label" for="vc-new-agency">Agency / CA firm <span class="muted">(optional)</span></label>
        <input class="input" id="vc-new-agency" name="agency_or_firm_name" maxlength="200" autocomplete="off"
          placeholder="e.g. Economic Offences Wing / Mehta &amp; Associates" />
      </div>
    </div>`,
    actions: [{ label: "Cancel", value: "cancel" }, { label: "Create case", variant: "primary", type: "submit" }],
    initialFocus: "#vc-new-title",
    onSubmit: async (v) => {
      const title = String(v.title || "").trim();
      if (!title) return false;
      const created = await store.createCase({
        title,
        mode: v.mode || defaultMode,
        agency_or_firm_name: String(v.agency_or_firm_name || "").trim() || null,
      });
      toast.success(`“${created.title}” created. Upload statements to begin.`, { title: "Case created" });
      await onCreated?.(created);
      return created;
    },
  });
  return m.closed;
}

export function mount(el, ctx) {
  const { store } = ctx;
  const statCache = new Map(); // case_id -> { status: "loading"|"ready"|"error", data, err }
  const statQueue = [];
  let active = 0;
  let filters = {
    q: ctx.params.q || "",
    mode: ["cypher_edge", "cypher_trace"].includes(ctx.params.mode) ? ctx.params.mode : "",
    sort: ctx.params.sort === "title" ? "title" : "recent",
  };
  const lastCase = lsGet(STORAGE_KEYS.lastCase);

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Cases",
        sub: "Client engagements (CypherEdge) and investigations (CypherTrace) held in the local, offline vault.",
        actions: html`<button type="button" class="btn btn--primary" data-action="new-case">${icon("plus", { size: "sm" })}New case</button>`,
      })}
      <div data-slot="totals" aria-live="polite"></div>
      <div class="toolbar v-cases-toolbar" role="search" aria-label="Filter cases">
        <div class="input-group">
          ${icon("search", { size: "sm" })}
          <input class="input" type="search" id="vc-q" data-filter="q" placeholder="Search title, agency or case ID"
            aria-label="Search cases" value="${filters.q}" autocomplete="off" />
        </div>
        <div class="seg" role="radiogroup" aria-label="Workspace">
          ${MODE_FILTERS.map(
            (f) => html`<button type="button" class="seg__btn" role="radio" data-mode="${f.value}"
              aria-checked="${filters.mode === f.value ? "true" : "false"}">${f.label}</button>`
          )}
        </div>
        <span class="spacer"></span>
        <label class="sr-only" for="vc-sort">Sort cases</label>
        <select class="select input--w-md" id="vc-sort" data-filter="sort">
          ${SORTS.map((s) => html`<option value="${s.value}" ${filters.sort === s.value ? html`selected` : ""}>${s.label}</option>`)}
        </select>
      </div>
      <div data-slot="list"></div>
      <p class="sr-only" aria-live="polite" data-slot="count"></p>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);

  // ---------------------------------------------------------------- per-case stats (bounded concurrency)
  function pump() {
    while (active < STAT_CONCURRENCY && statQueue.length && !ctx.signal.aborted) {
      const id = statQueue.shift();
      active += 1;
      api
        .get(caseUrl(id, "overview"), { params: { tx_limit: 0 }, signal: ctx.signal })
        .then((d) => statCache.set(id, { status: "ready", data: d }))
        .catch((err) => {
          if (isAbort(err)) return;
          statCache.set(id, { status: "error", err });
        })
        .finally(() => {
          active -= 1;
          if (!ctx.isCurrent()) return;
          patchStats(id);
          drawTotals();
          pump();
        });
    }
  }
  function requestStats(id, { force = false } = {}) {
    const cur = statCache.get(id);
    if (cur && !force && cur.status !== "error") return;
    if (cur?.status === "loading") return;
    statCache.set(id, { status: "loading" });
    statQueue.push(id);
    pump();
  }

  // ---------------------------------------------------------------- rendering
  function statsBody(c) {
    const st = statCache.get(c.case_id);
    if (!st || st.status === "loading") {
      return html`<div class="v-cases-stats" aria-busy="true" aria-label="Loading case figures">
        ${[0, 1, 2, 3].map(() => html`<span class="skeleton skeleton--line v-cases-stat-skel"></span>`)}</div>`;
    }
    if (st.status === "error") {
      return html`<div class="v-cases-stats v-cases-stats--error small">
        ${icon("alert", { size: "sm" })}<span>Figures unavailable${st.err?.status ? ` (${st.err.status})` : ""}</span>
        <button type="button" class="btn btn--ghost btn--sm" data-action="retry-stats" data-case="${c.case_id}">Retry</button></div>`;
    }
    const s = st.data.summary || {};
    const stmts = st.data.statements || [];
    const mismatches = stmts.reduce((a, x) => a + (Number(x.balance_mismatch_count) || 0), 0);
    const periods = stmts.map((x) => x.period_start).filter(Boolean).sort();
    const periodEnds = stmts.map((x) => x.period_end).filter(Boolean).sort();
    const empty = !s.statements_count;
    if (empty) {
      return html`<div class="v-cases-stats v-cases-stats--empty small muted">${icon("upload", { size: "sm" })}No statements ingested yet</div>`;
    }
    return html`<dl class="v-cases-stats">
        <div><dt>Statements</dt><dd class="tabular">${int(s.statements_count)}</dd></div>
        <div><dt>Rows</dt><dd class="tabular">${int(s.transactions_count)}</dd></div>
        <div><dt>Credits</dt><dd class="tabular is-credit" title="${inr(s.total_credits)}">${inrCompact(s.total_credits)}</dd></div>
        <div><dt>Debits</dt><dd class="tabular is-debit" title="${inr(s.total_debits)}">${inrCompact(s.total_debits)}</dd></div>
      </dl>
      <div class="v-cases-flags">
        ${periods.length ? html`<span class="xsmall muted" title="Statement coverage">${icon("calendar", { size: "sm" })}${date(periods[0])} → ${date(periodEnds[periodEnds.length - 1])}</span>` : ""}
        <span class="spacer"></span>
        ${s.suspense_count ? html`<span class="badge badge--warn" title="Suspense rows awaiting categorisation">${int(s.suspense_count)} suspense</span>` : ""}
        ${s.anomaly_count ? html`<span class="badge badge--danger" title="Rows flagged by the data-quality gate">${int(s.anomaly_count)} flagged</span>` : ""}
        ${mismatches ? html`<span class="badge badge--danger" title="Running-balance mismatches across statements">${int(mismatches)} mismatch</span>` : ""}
        ${!s.suspense_count && !s.anomaly_count && !mismatches ? html`<span class="badge badge--success">${icon("check", { size: "sm" })}Clean</span>` : ""}
      </div>`;
  }

  function cardHtml(c) {
    const open = href("overview", { caseId: c.case_id });
    const isLast = c.case_id === lastCase;
    return html`<article class="card v-cases-card ${isLast ? "v-cases-card--last" : ""}" data-case="${c.case_id}" aria-labelledby="vc-t-${c.case_id}">
      <div class="card__body stack-sm">
        <div class="row-between">
          ${modeBadge(c.mode)}
          <span class="xsmall muted" title="${date(c.created_at)}">${isLast ? html`<span class="badge badge--outline">Last opened</span> ` : ""}${relTime(c.created_at)}</span>
        </div>
        <h2 class="v-cases-card__title" id="vc-t-${c.case_id}"><a class="v-cases-card__link" href="${open}">${c.title}</a></h2>
        <div class="small text-2 truncate" title="${c.agency_or_firm_name || ""}">${c.agency_or_firm_name || CASE_MODES[c.mode]?.desc || ""}</div>
        <div class="v-cases-card__stats" data-stats="${c.case_id}">${statsBody(c)}</div>
      </div>
      <div class="card__foot v-cases-card__foot">
        <span class="mono xsmall truncate" title="Case ID">${c.case_id}</span>
        <span class="spacer"></span>
        <a class="btn btn--ghost btn--sm" href="${href("ingest", { caseId: c.case_id })}">${icon("upload", { size: "sm" })}Ingest</a>
        ${(SHORTCUTS[c.mode] || [])
          .filter((s) => primaryViews(c.mode).includes(s.view))
          .slice(0, 1)
          .map((s) => html`<a class="btn btn--ghost btn--sm" href="${href(s.view, { caseId: c.case_id })}">${s.label}</a>`)}
      </div>
    </article>`;
  }

  function visibleCases() {
    const q = filters.q.trim().toLowerCase();
    const list = (store.get().cases || []).filter(
      (c) =>
        (!filters.mode || c.mode === filters.mode) &&
        (!q || [c.title, c.agency_or_firm_name, c.case_id].some((v) => String(v || "").toLowerCase().includes(q)))
    );
    if (filters.sort === "title") list.sort((a, b) => String(a.title).localeCompare(String(b.title)));
    else list.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    return list;
  }

  function drawList() {
    const all = store.get().cases || [];
    const list = visibleCases();
    const host = slot("list");
    slot("count").textContent = `${plural(list.length, "case")} shown of ${all.length}.`;
    if (!all.length) {
      render(
        host,
        html`<div class="card">${emptyState({
          icon: "folder",
          title: "No cases yet",
          message: "Create a CypherEdge engagement (Tally, ITR, loan appraisal) or a CypherTrace investigation (fund trail, mule scoring) to start.",
          action: { label: "New case", id: "new-case" },
        })}</div>`
      );
      return;
    }
    if (!list.length) {
      render(
        host,
        html`<div class="card">${emptyState({
          icon: "search",
          title: "No cases match",
          message: "Try a different search term or workspace filter.",
          action: { label: "Clear filters", id: "clear-filters" },
          compact: true,
        })}</div>`
      );
      return;
    }
    // keyed patch: reuse existing card nodes, create only new ones, keep order
    let grid = host.querySelector(".v-cases-grid");
    if (!grid) {
      host.replaceChildren();
      grid = document.createElement("div");
      grid.className = "v-cases-grid";
      grid.setAttribute("role", "list");
      grid.setAttribute("aria-label", "Cases");
      host.appendChild(grid);
    }
    const existing = new Map(Array.from(grid.children).map((n) => [n.dataset.key, n]));
    const frag = document.createDocumentFragment();
    for (const c of list) {
      let node = existing.get(c.case_id);
      const sig = `${c.title}|${c.mode}|${c.agency_or_firm_name || ""}`;
      if (!node || node.dataset.sig !== sig) {
        node = document.createElement("div");
        node.setAttribute("role", "listitem");
        node.dataset.key = c.case_id;
        node.dataset.sig = sig;
        render(node, cardHtml(c));
      }
      existing.delete(c.case_id);
      frag.appendChild(node);
      requestStats(c.case_id);
    }
    grid.replaceChildren(frag);
  }

  function patchStats(id) {
    const c = (store.get().cases || []).find((x) => x.case_id === id);
    const target = el.querySelector(`[data-stats="${CSS.escape(id)}"]`);
    if (c && target) render(target, statsBody(c));
  }

  function drawTotals() {
    const cases = store.get().cases || [];
    const edge = cases.filter((c) => c.mode === "cypher_edge").length;
    const ready = cases.map((c) => statCache.get(c.case_id)).filter((s) => s && s.status === "ready");
    const pending = cases.filter((c) => !statCache.get(c.case_id) || statCache.get(c.case_id).status === "loading").length;
    const sum = (k) => ready.reduce((a, s) => a + (Number(s.data.summary?.[k]) || 0), 0);
    const partial = pending > 0 ? ` (${int(pending)} loading)` : "";
    render(
      slot("totals"),
      kpiGrid(
        [
          { label: "Cases", value: int(cases.length), sub: `${int(edge)} Edge · ${int(cases.length - edge)} Trace`, icon: "folder" },
          { label: "Statements", value: int(sum("statements_count")), sub: `across all cases${partial}`, icon: "file" },
          { label: "Transactions", value: int(sum("transactions_count")), sub: `ingested rows${partial}`, icon: "list" },
          {
            label: "Suspense pending",
            value: int(sum("suspense_count")),
            sub: "awaiting categorisation",
            tone: sum("suspense_count") ? "warn" : "",
            icon: "inbox",
          },
          {
            label: "Flagged rows",
            value: int(sum("anomaly_count")),
            sub: "data-quality gate",
            tone: sum("anomaly_count") ? "danger" : "",
            icon: "alert",
          },
        ],
        { compact: true, label: "Vault totals" }
      )
    );
  }

  // ---------------------------------------------------------------- interactions
  const syncUrl = () => ctx.setParams({ q: filters.q || null, mode: filters.mode || null, sort: filters.sort === "recent" ? null : filters.sort });
  const onSearch = debounce(() => {
    drawList();
    syncUrl();
  }, 180);
  ctx.onCleanup(() => onSearch.cancel());

  const newCase = () =>
    openCreateCaseDialog({
      store,
      defaultMode: filters.mode || "cypher_trace",
      onCreated: (created) => ctx.navigate("ingest", null, { caseId: created.case_id }),
    });

  const offs = [
    delegate(el, "click", '[data-action="new-case"]', newCase),
    delegate(el, "click", '[data-action="clear-filters"]', () => {
      filters = { ...filters, q: "", mode: "" };
      el.querySelector("#vc-q").value = "";
      syncSeg();
      drawList();
      syncUrl();
    }),
    delegate(el, "click", '[data-action="retry-stats"]', (e, b) => {
      requestStats(b.dataset.case, { force: true });
      patchStats(b.dataset.case);
    }),
    delegate(el, "click", "[data-mode]", (e, b) => {
      filters.mode = b.dataset.mode;
      syncSeg();
      drawList();
      syncUrl();
    }),
    delegate(el, "keydown", '[role="radio"][data-mode]', (e, b) => {
      if (!["ArrowRight", "ArrowLeft"].includes(e.key)) return;
      e.preventDefault();
      const btns = Array.from(el.querySelectorAll('[role="radio"][data-mode]'));
      const i = btns.indexOf(b) + (e.key === "ArrowRight" ? 1 : -1);
      const next = btns[(i + btns.length) % btns.length];
      next.focus();
      next.click();
    }),
    delegate(el, "input", "#vc-q", (e, inp) => {
      filters.q = inp.value;
      onSearch();
    }),
    delegate(el, "change", "#vc-sort", (e, sel) => {
      filters.sort = sel.value;
      drawList();
      syncUrl();
    }),
    // whole card is clickable (stretched title link); keep inner links/buttons working
    delegate(el, "click", ".v-cases-card", (e, card) => {
      if (e.target.closest("a, button, input, select")) return;
      if (window.getSelection && String(window.getSelection()).length) return;
      card.querySelector(".v-cases-card__link")?.click();
    }),
  ];
  ctx.onCleanup(() => offs.forEach((f) => f()));

  function syncSeg() {
    el.querySelectorAll("[data-mode]").forEach((b) => {
      const on = b.dataset.mode === filters.mode;
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
  }

  ctx.registerCommands([
    { id: "cases.new", title: "Create a new case…", group: "Cases", icon: "plus", keywords: ["workspace", "investigation", "engagement"], run: newCase },
    { id: "cases.search", title: "Search cases", group: "Cases", icon: "search", run: () => el.querySelector("#vc-q")?.focus() },
    { id: "cases.edge", title: "Show CypherEdge cases", group: "Cases", icon: "receipt", run: () => el.querySelector('[data-mode="cypher_edge"]')?.click() },
    { id: "cases.trace", title: "Show CypherTrace cases", group: "Cases", icon: "network", run: () => el.querySelector('[data-mode="cypher_trace"]')?.click() },
  ]);

  // ---------------------------------------------------------------- data
  syncSeg();
  drawList();
  drawTotals();
  ctx.onCleanup(
    store.select(
      (s) => s.cases,
      () => {
        if (!ctx.isCurrent()) return;
        drawList();
        drawTotals();
      }
    )
  );
  // another view mutated a case (upload, resolve…): refresh only that case's figures when we come back
  ctx.onData([TOPICS.cases], () => store.loadCases().catch((err) => toast.error(err, { title: "Could not refresh cases" })));
  store.loadCases().catch((err) => {
    if (!isAbort(err)) toast.error(err, { title: "Could not load cases" });
  });

  return {
    update(params) {
      filters = {
        q: params.q || "",
        mode: ["cypher_edge", "cypher_trace"].includes(params.mode) ? params.mode : "",
        sort: params.sort === "title" ? "title" : "recent",
      };
      const q = el.querySelector("#vc-q");
      if (q) q.value = filters.q;
      const s = el.querySelector("#vc-sort");
      if (s) s.value = filters.sort;
      syncSeg();
      drawList();
    },
  };
}
