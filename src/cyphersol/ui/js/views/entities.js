/* Entity resolution (#/case/{id}/entities) — legacy E1–E3, report NLP-11 / NLP-12 / UI-S-14 / §5.3 item 11.
 *
 *   - entities grid: canonical name, status (has statement / details only / system), linked accounts & VPAs,
 *     aliases, credited / debited, txns; system (UNRESOLVED_*) rows hidden unless toggled on
 *   - similarity slider 0.50–1.00 (from case.similarity_threshold) with a server dry run
 *     (GET entities/preview?threshold=): merges / splits against the stored entities, holder-guard and
 *     PAN conflicts in plain words; Re-cluster (POST entities/recluster) behind a confirmation
 *   - cluster review: near-miss pairs (with IDs) -> merge (POST entities/merge) or "not the same"
 *     (POST entities/reject); drawer -> split aliases off (POST entities/split) or merge into another entity;
 *     holder-guard warning before merging two statement holders
 *   - decisions panel: active decisions with per-decision undo and "undo last" (POST entities/undo),
 *     append-only history (GET entities/decisions)
 *   - manual merge by name (resolved to entity IDs; unknown alias text falls back to the legacy rule)
 * After every mutation: await ctx.mutated(MUTATION_TOPICS.recluster), then patch grid rows / panels.
 * Requests that can be superseded (preview, decisions) use their own AbortController linked to ctx.signal.
 * URL params: q (search), status (statement|details), system (true).
 */
import { entityStatus } from "../core/constants.js";
import { isAbort, linkAbort } from "../core/api.js";
import { debounce, delegate, html, render } from "../core/dom.js";
import { DASH, dateTime, inr, inrCompact, int, num, plural } from "../core/format.js";
import { MUTATION_TOPICS, TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { openDrawer } from "../components/drawer.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { confirmDialog, openModal } from "../components/modal.js";
import { emptyState, pageHead, spinner } from "../components/states.js";
import { toast, withBusy } from "../components/toast.js";
import {
  candidatePairs, decisionText, evidenceText, historyLabel, holderGuardWarning, opLabel, previewSummary, splitError,
} from "./entities-review.js";

export const styles = ["views/risk.css"];

const isSystem = (e) => entityStatus(e).cls === "system";
const MAX_LIST = 8;

export async function mount(el, ctx) {
  await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return undefined;

  const caseThreshold = () => {
    const c = ctx.store.get().caseData?.case || ctx.store.get().case || ctx.case || {};
    const t = Number(c.similarity_threshold);
    return Number.isFinite(t) ? t : 0.85;
  };
  let threshold = caseThreshold();

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Entities",
        sub: "Counterparties resolved across every statement: aliases, linked accounts and VPAs, statement status and analyst review decisions.",
      })}
      <div data-slot="kpis"></div>
      <div class="v-entities-tools">
        <section class="card" aria-labelledby="en-res">
          <div class="card__head"><div><h2 class="card__title" id="en-res">Resolution threshold</h2>
            <p class="card__sub">Fuzzy-name similarity needed to cluster two spellings into one entity. Moving the slider runs a dry run on the server; nothing changes until you re-cluster.</p></div></div>
          <div class="card__body stack-sm">
            <div class="field">
              <div class="row-between"><label class="field__label" for="en-thr">Similarity</label>
                <output class="v-entities-thr tabular" for="en-thr" data-slot="thr-out"></output></div>
              <input id="en-thr" type="range" min="0.5" max="1" step="0.01" data-slot="thr" aria-describedby="en-thr-hint" />
              <div class="row-between xsmall muted" aria-hidden="true"><span>0.50 · looser</span><span>1.00 · exact</span></div>
            </div>
            <div class="small text-2 stack-sm" id="en-thr-hint" data-slot="preview" aria-live="polite"></div>
            <div class="row-wrap">
              <button type="button" class="btn btn--primary" data-action="recluster">${icon("refresh", { size: "sm" })}Re-cluster entities</button>
              <button type="button" class="btn btn--ghost btn--sm" data-action="thr-reset">Reset</button>
            </div>
          </div>
        </section>
        <section class="card" aria-labelledby="en-near">
          <div class="card__head"><div><h2 class="card__title" id="en-near">Review candidates</h2>
            <p class="card__sub" data-slot="near-sub">Pairs scoring just below the threshold. Merge them if they are the same party, or mark them as different so they are never linked.</p></div></div>
          <div class="card__body card__body--flush" data-slot="near"></div>
        </section>
        <section class="card" aria-labelledby="en-merge">
          <div class="card__head"><div><h2 class="card__title" id="en-merge">Manual merge</h2>
            <p class="card__sub">Merge one entity into another. Saved as a review decision (undoable) and kept on every re-cluster.</p></div></div>
          <form class="card__body stack-sm" data-slot="merge-form" novalidate>
            <div class="field">
              <label class="field__label" for="en-alias">Entity or alias to absorb</label>
              <input id="en-alias" class="input" name="alias_name" list="en-names" autocomplete="off" required placeholder="e.g. R K MULE" />
            </div>
            <div class="field">
              <label class="field__label" for="en-target">Merge into (kept)</label>
              <input id="en-target" class="input" name="canonical_target" list="en-names" autocomplete="off" required placeholder="e.g. RAMESH KUMAR MULE" />
            </div>
            <datalist id="en-names" data-slot="names"></datalist>
            <p class="field__error small" data-slot="merge-err" role="alert" hidden></p>
            <div class="row-wrap"><button type="submit" class="btn">${icon("merge", { size: "sm" })}Merge</button></div>
          </form>
        </section>
        <section class="card" aria-labelledby="en-dec">
          <div class="card__head row-between"><div><h2 class="card__title" id="en-dec">Review decisions</h2>
            <p class="card__sub">Merges, splits and "not the same" decisions, oldest first. Undo restores the original entity IDs.</p></div>
            <button type="button" class="btn btn--sm" data-action="undo-last" disabled>${icon("arrow-left", { size: "sm" })}Undo last</button></div>
          <div class="card__body stack-sm" data-slot="decisions"></div>
        </section>
      </div>
      <section class="card" aria-labelledby="en-list">
        <div class="card__head"><div><h2 class="card__title" id="en-list">Resolved entities</h2>
          <p class="card__sub">Credited / debited are the amounts recorded against the entity as a counterparty in the case's statements. Open a row to split aliases or merge it.</p></div></div>
        <div class="card__body card__body--flush" data-slot="grid"></div>
      </section>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);
  const range = slot("thr");
  range.value = String(threshold);

  const allEntities = () => ctx.store.get().caseData?.entities || [];
  const byId = () => new Map(allEntities().map((e) => [e.entity_id, e]));
  const nameOf = (id) => byId().get(id)?.canonical_name || "";

  // server state: dry-run preview + decisions
  let preview = null;
  let previewErr = null;
  let previewLoading = false;
  let decisions = { decisions: [], history: [], legacy_manual_merges: [] };
  let decisionsErr = null;

  /** Fetch helper with its own AbortController (superseded requests are aborted). */
  const requester = () => {
    let ctl = null;
    let unlink = () => {};
    const run = async (fn) => {
      ctl?.abort();
      unlink();
      const mine = new AbortController();
      ctl = mine;
      unlink = linkAbort(ctx.signal, mine);
      try {
        return await fn(mine.signal);
      } finally {
        if (ctl === mine) {
          unlink();
          unlink = () => {};
        }
      }
    };
    run.abort = () => {
      ctl?.abort();
      unlink();
    };
    return run;
  };
  const previewReq = requester();
  const decisionsReq = requester();
  ctx.onCleanup(() => {
    previewReq.abort();
    decisionsReq.abort();
  });

  const nearCandidates = () => {
    const fromPreview = preview && Math.abs(Number(preview.threshold) - Number(range.value)) < 0.001 ? candidatePairs(preview.candidates) : null;
    return fromPreview || candidatePairs(ctx.store.get().caseData?.near_merge_candidates || []);
  };

  // ------------------------------------------------------------------ KPIs
  const drawKpis = () => {
    const ents = allEntities().filter((e) => !isSystem(e));
    const withStmt = ents.filter((e) => e.has_statement).length;
    const aliasesMerged = ents.reduce((s, e) => s + Math.max(0, (e.aliases || []).length - 1), 0);
    const sys = allEntities().length - ents.length;
    const cands = nearCandidates();
    render(
      slot("kpis"),
      kpiGrid(
        [
          { label: "Entities", value: int(ents.length), icon: "users", sub: sys ? `${int(sys)} system hidden` : "" },
          { label: "Has statement", value: int(withStmt), icon: "file" },
          { label: "Details only", value: int(ents.length - withStmt), sub: "seen as counterparty" },
          { label: "Aliases merged", value: int(aliasesMerged), icon: "merge" },
          { label: "Review pairs", value: int(cands.length), tone: cands.length ? "warn" : "success" },
          { label: "Decisions", value: int((decisions.decisions || []).length), sub: "merge / split / reject" },
          { label: "Similarity threshold", value: num(caseThreshold(), 2), tone: "accent" },
        ],
        { label: "Entity resolution summary" }
      )
    );
  };

  // ------------------------------------------------------------------ threshold preview (server dry run)
  const drawThrOut = () => {
    const v = Number(range.value);
    const cur = caseThreshold();
    render(slot("thr-out"), html`${num(v, 2)}${Math.abs(v - cur) > 0.001 ? html` <span class="muted small">(current ${num(cur, 2)})</span>` : ""}`);
    el.querySelector('[data-action="recluster"]').classList.toggle("btn--primary", Math.abs(v - cur) > 0.001);
  };

  const drawPreview = () => {
    drawThrOut();
    const v = Number(range.value);
    const box = slot("preview");
    if (previewErr) {
      render(box, html`<div class="callout callout--danger">${icon("x-circle")}<div>Could not preview this threshold: ${previewErr.message || "request failed"}</div></div>`);
      return;
    }
    if (!preview || Math.abs(Number(preview.threshold) - v) > 0.001) {
      render(box, html`<p class="row">${spinner("Previewing")}<span>Previewing threshold ${num(v, 2)}…</span></p>`);
      return;
    }
    const s = previewSummary(preview, nameOf);
    const same = Math.abs(v - caseThreshold()) < 0.001;
    render(
      box,
      html`<p><strong>${s.headline}</strong> At ${num(v, 2)}: ${plural(s.entityCount, "entity", "entities")}, ${plural(s.multiAlias || 0, "multi-alias cluster")}${same ? " (the case's current threshold)" : ""}.</p>
        ${s.merged.length
          ? html`<div><h3 class="subhead">Would merge</h3><ul class="v-entities-plist">${s.merged.slice(0, MAX_LIST).map((m) => html`<li>${m.from.join(" + ")} <span class="muted">→ ${m.name}</span></li>`)}</ul>
              ${s.merged.length > MAX_LIST ? html`<p class="xsmall muted">and ${plural(s.merged.length - MAX_LIST, "more merge")}.</p>` : ""}</div>`
          : ""}
        ${s.split.length
          ? html`<div><h3 class="subhead">Would split</h3><ul class="v-entities-plist">${s.split.slice(0, MAX_LIST).map((m) => html`<li>${m.name} <span class="muted">→ ${m.into.join(", ")}</span></li>`)}</ul>
              ${s.split.length > MAX_LIST ? html`<p class="xsmall muted">and ${plural(s.split.length - MAX_LIST, "more split")}.</p>` : ""}</div>`
          : ""}
        ${s.conflicts.length
          ? html`<div class="stack-sm"><h3 class="subhead">Kept apart (${int(s.conflicts.length)})</h3>
              ${s.conflicts.slice(0, MAX_LIST).map((c) => html`<div class="callout callout--${c.tone === "warn" ? "warn" : "info"} v-entities-conflict">${icon(c.tone === "warn" ? "shield" : "info")}<div>${c.message}</div></div>`)}
              ${s.conflicts.length > MAX_LIST ? html`<p class="xsmall muted">and ${plural(s.conflicts.length - MAX_LIST, "more conflict")}.</p>` : ""}</div>`
          : ""}`
    );
  };

  const loadPreview = async () => {
    const v = Number(range.value);
    previewLoading = true;
    previewErr = null;
    drawPreview();
    try {
      const p = await previewReq((signal) => ctx.api.get("entities/preview", { threshold: v.toFixed(2) }, { signal }));
      if (!ctx.isCurrent()) return;
      preview = p;
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      previewErr = err;
    } finally {
      previewLoading = false;
    }
    if (Math.abs(Number(range.value) - v) > 0.001) return; // superseded by a newer slider value
    drawPreview();
    drawNear();
    drawKpis();
  };
  const loadPreviewSoon = debounce(loadPreview, 250);
  ctx.onCleanup(() => loadPreviewSoon.cancel());

  range.addEventListener("input", () => {
    drawThrOut();
    if (!previewLoading) render(slot("preview"), html`<p class="row">${spinner("Previewing")}<span>Previewing threshold ${num(Number(range.value), 2)}…</span></p>`);
    loadPreviewSoon();
  });
  delegate(el, "click", '[data-action="thr-reset"]', () => {
    range.value = String(caseThreshold());
    loadPreviewSoon.flush();
  });

  // ------------------------------------------------------------------ review candidates
  let nearList = [];
  const drawNear = () => {
    const cands = nearCandidates();
    nearList = cands;
    render(slot("near-sub"), `Pairs scoring just below ${num(Number(range.value), 2)}. Merge them if they are the same party, or mark them as different so they are never linked.`);
    if (!cands.length) {
      render(slot("near"), emptyState({ icon: "check", title: "No candidates to review", message: "No pairs score just below this threshold.", compact: true, tone: "success" }));
      return;
    }
    const ents = byId();
    const nm = (id, fallback) => ents.get(id)?.canonical_name || fallback || id.slice(0, 8);
    render(
      slot("near"),
      html`<ul class="v-entities-near" aria-label="Candidate pairs to review">${cands.slice(0, 50).map((c, i) => {
        const a = nm(c.entity_a_id, c.entity_a);
        const b = nm(c.entity_b_id, c.entity_b);
        const guard = holderGuardWarning(ents.get(c.entity_a_id), ents.get(c.entity_b_id));
        return html`<li class="v-entities-near__row">
          <div class="min-w-0">
            <div class="v-entities-near__pair"><span class="truncate" title="${c.name_a || a}">${a}</span><span class="muted" aria-hidden="true">⇄</span><span class="sr-only"> and </span><span class="truncate" title="${c.name_b || b}">${b}</span></div>
            <div class="row xsmall muted">
              <svg class="v-entities-near__bar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true" focusable="false">
                <rect class="v-risk-bars__bg" x="0" y="0" width="100" height="4"></rect>
                <rect class="fill-1" x="0" y="0" width="${Math.max(1, Math.min(100, (Number(c.similarity) || 0) * 100))}" height="4"></rect>
              </svg><span class="tabular">similarity ${num(c.similarity, 3)}</span>${guard ? html`<span class="badge badge--warn" title="${guard}">${icon("shield", { size: "sm" })}Both hold statements</span>` : ""}</div>
          </div>
          <div class="row v-entities-near__act">
            <button type="button" class="btn btn--sm" data-action="near-merge" data-i="${i}" data-dir="ab" title="Merge ${b} into ${a}" aria-label="Merge ${b} into ${a}">${icon("merge", { size: "sm" })}Into A</button>
            <button type="button" class="btn btn--sm btn--ghost" data-action="near-merge" data-i="${i}" data-dir="ba" title="Merge ${a} into ${b}" aria-label="Merge ${a} into ${b}">Into B</button>
            <button type="button" class="btn btn--sm btn--ghost" data-action="near-reject" data-i="${i}" title="Mark ${a} and ${b} as different parties" aria-label="Mark ${a} and ${b} as different parties">${icon("x", { size: "sm" })}Not same</button>
          </div>
        </li>`;
      })}</ul>${cands.length > 50 ? html`<p class="xsmall muted v-entities-pad">Showing the 50 most similar of ${int(cands.length)} pairs.</p>` : ""}`
    );
  };

  const drawNames = () => {
    const names = new Set();
    allEntities().forEach((e) => {
      if (isSystem(e)) return;
      names.add(e.canonical_name);
      (e.aliases || []).forEach((a) => names.add(a));
    });
    render(slot("names"), html`${Array.from(names).sort().slice(0, 2000).map((n) => html`<option value="${n}"></option>`)}`);
  };

  // ------------------------------------------------------------------ decisions + history
  const drawDecisions = () => {
    const box = slot("decisions");
    const list = decisions.decisions || [];
    const hist = decisions.history || [];
    const legacy = decisions.legacy_manual_merges || [];
    el.querySelector('[data-action="undo-last"]').disabled = !list.length;
    if (decisionsErr) {
      render(box, html`<div class="callout callout--danger">${icon("x-circle")}<div>Could not load review decisions: ${decisionsErr.message || "request failed"} <button type="button" class="btn btn--sm btn--ghost" data-action="dec-retry">Retry</button></div></div>`);
      return;
    }
    render(
      box,
      html`${list.length
          ? html`<ol class="v-entities-dec" aria-label="Active review decisions">${list.map((d) => html`<li class="v-entities-dec__row">
              <div class="min-w-0"><div><span class="badge badge--${d.op === "reject" ? "neutral" : d.op === "split" ? "info" : "accent"}">${opLabel(d.op)}</span> ${decisionText(d, nameOf)}</div>
                <div class="xsmall muted">${d.actor || "analyst"} · ${dateTime(d.created_at)}${d.note ? html` · “${d.note}”` : ""}</div></div>
              <button type="button" class="btn btn--sm btn--ghost" data-action="undo" data-id="${d.decision_id}" aria-label="Undo: ${decisionText(d, nameOf)}">${icon("arrow-left", { size: "sm" })}Undo</button>
            </li>`)}</ol>`
          : emptyState({ icon: "inbox", title: "No review decisions yet", message: "Merges, splits and rejected pairs you make here are listed with an undo.", compact: true })}
        ${legacy.length
          ? html`<p class="xsmall muted">${plural(legacy.length, "legacy name rule")} also apply: ${legacy.slice(0, 5).map((m, i) => html`${i ? "; " : ""}${m.alias} → ${m.canonical}`)}${legacy.length > 5 ? "…" : ""}</p>`
          : ""}
        ${hist.length
          ? html`<details class="v-entities-hist"><summary class="small">History (${int(hist.length)})</summary>
              <ol class="v-entities-dec">${hist.slice(0, 100).map((h) => html`<li class="v-entities-dec__row"><div class="min-w-0">
                <div><span class="badge badge--neutral">${historyLabel(h.action)}</span> ${decisionText(h.decision || {}, nameOf)}</div>
                <div class="xsmall muted">${h.actor || "analyst"} · ${dateTime(h.created_at)}</div></div></li>`)}</ol></details>`
          : ""}`
    );
  };

  const loadDecisions = async () => {
    try {
      const d = await decisionsReq((signal) => ctx.api.get("entities/decisions", null, { signal }));
      if (!ctx.isCurrent()) return;
      decisions = d || decisions;
      decisionsErr = null;
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      decisionsErr = err;
    }
    drawDecisions();
    drawKpis();
  };
  delegate(el, "click", '[data-action="dec-retry"]', () => loadDecisions());

  // ------------------------------------------------------------------ grid
  const grid = createDataGrid(slot("grid"), {
    ariaLabel: "Resolved entities",
    rowKey: "entity_id",
    rows: allEntities(),
    rowHeight: 50,
    height: "min(64vh, 620px)",
    countNoun: ["entity", "entities"],
    initialSort: { key: ctx.params.sort || "total_credited", order: ctx.params.order || "desc" },
    initialFilters: { q: ctx.params.q || "", status: ctx.params.status || "", system: ctx.params.system || "" },
    filters: [
      { key: "q", type: "search", label: "Search entities", placeholder: "Name, alias, account or VPA…" },
      { key: "status", type: "select", label: "Statement status", options: [{ value: "", label: "All statuses" }, { value: "statement", label: "Has statement" }, { value: "details", label: "Details only" }] },
      { key: "system", type: "toggle", label: "Show system entities" },
    ],
    clientFilter: (e, f) => {
      const st = entityStatus(e).cls;
      if (st === "system" && !f.system) return false;
      if (f.status && st !== f.status) return false;
      if (f.q) {
        const q = String(f.q).toLowerCase();
        const hay = [e.canonical_name, ...(e.aliases || []), ...(e.linked_account_numbers || []), ...(e.linked_vpas || [])];
        return hay.some((v) => String(v || "").toLowerCase().includes(q));
      }
      return true;
    },
    onStateChange: ({ sort, order, filters }) => ctx.setParams({ q: filters.q || null, status: filters.status || null, system: filters.system || null, sort, order }),
    onRowClick: (e) => openEntity(e),
    emptyState: { icon: "users", title: "No entities yet", message: "Entities are resolved automatically when statements are ingested.", action: { label: "Upload statements", href: ctx.href("ingest") } },
    columns: [
      { key: "canonical_name", label: "Canonical name", width: "minmax(220px, 2fr)", sortable: true,
        render: (e) => html`<span class="strong truncate">${e.canonical_name}</span><span class="dg__sub">${plural((e.aliases || []).length, "alias", "aliases")}${e.is_moved_to_misc ? " · moved to Misc" : ""}</span>` },
      { key: "has_statement", label: "Status", width: "128px", sortable: true, sortValue: (e) => entityStatus(e).label,
        render: (e) => { const st = entityStatus(e); return html`<span class="badge badge--${st.cls}">${st.label}</span>`; } },
      { key: "linked_account_numbers", label: "Accounts / VPAs", width: "minmax(180px, 1.4fr)", sortable: true, sortValue: (e) => (e.linked_account_numbers || []).length + (e.linked_vpas || []).length,
        title: (e) => [...(e.linked_account_numbers || []), ...(e.linked_vpas || [])].join(", "),
        render: (e) => {
          const accts = e.linked_account_numbers || [];
          const vpas = e.linked_vpas || [];
          if (!accts.length && !vpas.length) return html`<span class="muted">${DASH}</span>`;
          return html`<span class="mono truncate">${accts.join(", ") || DASH}</span><span class="dg__sub mono">${vpas.length ? vpas.join(", ") : "no VPA"}</span>`;
        } },
      { key: "aliases", label: "Aliases", width: "minmax(200px, 2fr)", sortable: true, sortValue: (e) => (e.aliases || []).length, title: (e) => (e.aliases || []).join(" · "),
        render: (e) => html`<span class="truncate text-2">${(e.aliases || []).join(" · ") || DASH}</span>` },
      { key: "total_credited", label: "Credited", width: "120px", align: "right", sortable: true, defaultOrder: "desc", className: "is-credit tabular", title: (e) => inr(e.total_credited), format: (v) => inrCompact(v) },
      { key: "total_debited", label: "Debited", width: "120px", align: "right", sortable: true, defaultOrder: "desc", className: "is-debit tabular", title: (e) => inr(e.total_debited), format: (v) => inrCompact(v) },
      { key: "tx_count", label: "Txns", width: "68px", align: "right", sortable: true, defaultOrder: "desc", className: "tabular", format: (v) => int(v) },
      { key: "_act", label: "Open", width: "96px",
        render: (e) => (isSystem(e) ? html`<span class="muted small">System</span>` : html`<span class="row">
          <a class="btn btn--ghost btn--sm btn--icon" href="${ctx.href("profile", { entity: e.entity_id })}" aria-label="Profile of ${e.canonical_name}" title="Financial profile">${icon("user", { size: "sm" })}</a>
          <a class="btn btn--ghost btn--sm btn--icon" href="${ctx.href("network", { entity: e.entity_id })}" aria-label="${e.canonical_name} in network graph" title="Network">${icon("network", { size: "sm" })}</a></span>`) },
    ],
  });
  ctx.onCleanup(() => grid.destroy());

  // ------------------------------------------------------------------ entity drawer
  let drawer = null;
  ctx.onCleanup(() => drawer?.close());
  function openEntity(e) {
    const st = entityStatus(e);
    const evidence = (preview?.clusters || []).find((c) => c.entity_id === e.entity_id)?.evidence || [];
    drawer = openDrawer({
      title: e.canonical_name,
      width: 520,
      className: "v-entities-drawer",
      body: html`<div class="stack">
        <div class="row-wrap"><span class="badge badge--${st.cls}">${st.label}</span>${e.is_moved_to_misc ? html`<span class="badge badge--misc">Moved to Misc</span>` : ""}
          <span class="xsmall muted mono">${e.entity_id}</span></div>
        <dl class="kv">
          <dt>Credited</dt><dd class="tabular is-credit">${inr(e.total_credited)}</dd>
          <dt>Debited</dt><dd class="tabular is-debit">${inr(e.total_debited)}</dd>
          <dt>Transactions</dt><dd class="tabular">${int(e.tx_count)}</dd>
          <dt>Accounts</dt><dd>${(e.linked_account_numbers || []).length ? e.linked_account_numbers.map((a) => html`<div class="mono">${a}</div>`) : html`<span class="muted">None linked</span>`}</dd>
          <dt>VPAs</dt><dd>${(e.linked_vpas || []).length ? e.linked_vpas.map((v) => html`<div class="mono">${v}</div>`) : html`<span class="muted">None</span>`}</dd>
        </dl>
        <div><h3 class="subhead">Aliases (${int((e.aliases || []).length)})</h3>
          <ul class="v-entities-aliases">${(e.aliases || []).map((a) => html`<li><span class="badge badge--neutral">${a}</span></li>`)}</ul></div>
        ${evidence.length
          ? html`<div><h3 class="subhead">Why these spellings were linked</h3>
              <ul class="v-entities-plist small">${evidence.slice(0, 20).map((ev) => html`<li>${ev.a} ⇄ ${ev.b} <span class="muted">(${evidenceText(ev)})</span></li>`)}</ul></div>`
          : ""}
        ${isSystem(e) ? "" : html`<div class="row-wrap">
          <a class="btn btn--sm" href="${ctx.href("profile", { entity: e.entity_id })}">${icon("user", { size: "sm" })}Profile</a>
          <a class="btn btn--sm" href="${ctx.href("network", { entity: e.entity_id })}">${icon("network", { size: "sm" })}Network</a>
          <a class="btn btn--sm" href="${ctx.href("layering", { source: e.entity_id })}">${icon("layers", { size: "sm" })}Layering</a>
          <a class="btn btn--sm" href="${ctx.href("transactions", { entity: e.entity_id })}">${icon("list", { size: "sm" })}Transactions</a>
        </div>
        <div class="row-wrap">
          <button type="button" class="btn btn--sm btn--ghost" data-action="merge-from">${icon("merge", { size: "sm" })}Merge into another entity…</button>
          ${(e.aliases || []).length > 1 ? html`<button type="button" class="btn btn--sm btn--ghost" data-action="split-from">${icon("swap", { size: "sm" })}Split aliases off…</button>` : ""}
        </div>`}
      </div>`,
    });
    const d = drawer;
    d.body.addEventListener("click", (ev) => {
      if (ev.target.closest("a[href]")) d.close();
      else if (ev.target.closest('[data-action="merge-from"]')) {
        d.close();
        openMergeDialog(e);
      } else if (ev.target.closest('[data-action="split-from"]')) {
        d.close();
        openSplitDialog(e);
      }
    });
  }

  // ------------------------------------------------------------------ mutations
  const countNow = () => allEntities().filter((x) => !isSystem(x)).length;

  /** After any decision / re-cluster: refresh store data, decisions and the preview. */
  async function afterMutation() {
    await ctx.mutated(MUTATION_TOPICS.recluster);
    if (!ctx.isCurrent()) return false;
    preview = null;
    await Promise.all([loadDecisions(), loadPreview()]);
    return ctx.isCurrent();
  }

  /** POST a review decision; toasts the result. Resolves true on success. */
  async function postDecision(sub, body, btn, successMsg) {
    const before = countNow();
    const res = await withBusy(btn, async () => {
      const r = await ctx.api.post(sub, body);
      await afterMutation();
      return r || {};
    }, { error: `${({ merge: "Merge", split: "Split", reject: "Reject", undo: "Undo" })[sub.split("/")[1]] || "Action"} failed` });
    if (!ctx.isCurrent()) return false;
    drawDecisions(); // withBusy re-enabled the button: restore "Undo last" to match the list
    if (!res) return false;
    const msg = typeof successMsg === "function" ? successMsg(res, { before, after: countNow() }) : successMsg;
    if (res.changed === false) toast.info("Nothing changed: the entities already matched this decision.", { title: "No change" });
    else toast.success(msg, { title: "Entities updated" });
    (res.conflicts || []).filter((c) => c.type === "HOLDER_GUARD").slice(0, 2).forEach((c) => toast.warn(c.reason || c.detail || "Holder guard kept two statement holders apart.", { title: "Holder guard" }));
    return true;
  }

  async function confirmMerge(source, target) {
    const guard = holderGuardWarning(source, target);
    return confirmDialog({
      title: guard ? "Merge two statement holders?" : "Merge entities?",
      message: `"${source.canonical_name}" will be merged into "${target.canonical_name}" (the kept ID). The decision is logged in the audit trail and can be undone.${guard ? ` ${guard}` : ""}`,
      confirmLabel: guard ? "Merge anyway" : "Merge",
      danger: !!guard,
    });
  }

  async function mergeIds(sourceId, targetId, btn, note = "") {
    const ents = byId();
    const s = ents.get(sourceId);
    const t = ents.get(targetId);
    if (!s || !t || s.entity_id === t.entity_id) {
      toast.error(s && t ? "Cannot merge an entity with itself." : "That entity no longer exists. Refresh and try again.", { title: "Merge failed" });
      return false;
    }
    if (!(await confirmMerge(s, t)) || !ctx.isCurrent()) return false;
    return postDecision("entities/merge", { source_entity_id: s.entity_id, target_entity_id: t.entity_id, note }, btn,
      (_r, { before, after }) => `Merged "${s.canonical_name}" into "${t.canonical_name}" (${int(before)} → ${int(after)} entities).`);
  }

  async function rejectIds(aId, bId, btn) {
    const ents = byId();
    const a = ents.get(aId);
    const b = ents.get(bId);
    if (!a || !b) return false;
    const ok = await confirmDialog({
      title: "Mark as different parties?",
      message: `"${a.canonical_name}" and "${b.canonical_name}" will never be linked automatically again, at any threshold. You can undo this from Review decisions.`,
      confirmLabel: "Not the same",
    });
    if (!ok || !ctx.isCurrent()) return false;
    return postDecision("entities/reject", { entity_a_id: a.entity_id, entity_b_id: b.entity_id }, btn,
      `"${a.canonical_name}" and "${b.canonical_name}" are kept apart.`);
  }

  function openMergeDialog(src) {
    const others = allEntities().filter((x) => !isSystem(x) && x.entity_id !== src.entity_id)
      .sort((a, b) => String(a.canonical_name).localeCompare(String(b.canonical_name)));
    const m = openModal({
      title: `Merge "${src.canonical_name}"`,
      description: "Choose the entity to keep. The merged entity keeps the target's ID; the decision can be undone.",
      body: html`<div class="stack-sm">
        <div class="field"><label class="field__label" for="en-mg-t">Merge into</label>
          <select id="en-mg-t" class="select" name="target" required>
            <option value="">Choose an entity…</option>
            ${others.map((o) => html`<option value="${o.entity_id}">${o.canonical_name}${o.has_statement ? " (has statement)" : ""}</option>`)}
          </select></div>
        <div class="field"><label class="field__label" for="en-mg-n">Note (optional)</label>
          <input id="en-mg-n" class="input" name="note" maxlength="300" autocomplete="off" placeholder="e.g. same PAN in KYC" /></div>
        <p class="small callout callout--warn" data-slot="mg-guard" hidden></p>
      </div>`,
      actions: [{ label: "Cancel", value: "cancel" }, { label: "Merge", variant: "primary", type: "submit" }],
      onOpen: (dlg) => {
        const sel = dlg.querySelector('[name="target"]');
        const guardEl = dlg.querySelector('[data-slot="mg-guard"]');
        sel.addEventListener("change", () => {
          const msg = holderGuardWarning(src, others.find((o) => o.entity_id === sel.value));
          guardEl.hidden = !msg;
          guardEl.textContent = msg || "";
        });
      },
      onSubmit: async (v) => {
        if (!v.target) throw "Choose the entity to merge into.";
        return v;
      },
    });
    m.closed.then((v) => {
      if (v && v.target && ctx.isCurrent()) mergeIds(src.entity_id, v.target, null, String(v.note || "").trim());
    });
  }

  function openSplitDialog(ent) {
    const aliases = ent.aliases || [];
    const m = openModal({
      title: `Split "${ent.canonical_name}"`,
      description: "Tick the aliases that belong to a different party. They become a new entity; the rest keep this entity's ID.",
      body: html`<fieldset class="stack-sm v-entities-split"><legend class="field__label">Aliases to split off</legend>
          ${aliases.map((a, i) => html`<label class="check"><input type="checkbox" name="a${i}" value="${a}" />${a}</label>`)}
        </fieldset>
        <div class="field"><label class="field__label" for="en-sp-n">Note (optional)</label>
          <input id="en-sp-n" class="input" name="note" maxlength="300" autocomplete="off" /></div>`,
      actions: [{ label: "Cancel", value: "cancel" }, { label: "Split", variant: "primary", type: "submit" }],
      onSubmit: async (v) => {
        const picked = aliases.filter((_a, i) => v[`a${i}`]);
        const err = splitError(aliases, picked);
        if (err) throw err;
        return { picked, note: String(v.note || "").trim() };
      },
    });
    m.closed.then((v) => {
      if (!v || !v.picked || !ctx.isCurrent()) return;
      postDecision("entities/split", { entity_id: ent.entity_id, aliases: v.picked, note: v.note }, null,
        `Split ${v.picked.map((x) => `"${x}"`).join(", ")} out of "${ent.canonical_name}".`);
    });
  }

  delegate(el, "click", '[data-action="recluster"]', async (e, btn) => {
    const v = Number(range.value);
    const cur = caseThreshold();
    const s = preview && Math.abs(Number(preview.threshold) - v) < 0.001 ? previewSummary(preview, nameOf) : null;
    const ok = await confirmDialog({
      title: "Re-cluster entities?",
      message: `Entity resolution will be re-run over every statement at similarity ${num(v, 2)}${Math.abs(v - cur) > 0.001 ? ` (currently ${num(cur, 2)})` : ""}.${s ? ` Preview: ${s.headline}` : ""} Review decisions are re-applied and unchanged entities keep their IDs. The run is logged in the audit trail.`,
      confirmLabel: "Re-cluster",
    });
    if (!ok || !ctx.isCurrent()) return;
    const before = countNow();
    const done = await withBusy(btn, async () => {
      await ctx.api.post("entities/recluster", { similarity_threshold: v });
      await afterMutation();
      return true;
    }, { error: "Re-cluster failed" });
    if (done && ctx.isCurrent()) toast.success(`${int(before)} → ${int(countNow())} entities at threshold ${num(v, 2)}.`, { title: "Entities re-clustered" });
  });

  /** Resolve a typed name to an entity: exact canonical name first, then a unique alias owner. */
  const findEntity = (name) => {
    const n = String(name || "").trim().toUpperCase();
    if (!n) return null;
    const ents = allEntities().filter((x) => !isSystem(x));
    return ents.find((x) => String(x.canonical_name).toUpperCase() === n)
      || (() => {
        const owners = ents.filter((x) => (x.aliases || []).some((a) => String(a).toUpperCase() === n));
        return owners.length === 1 ? owners[0] : null;
      })();
  };

  async function mergeByName(alias, target, btn) {
    const s = findEntity(alias);
    const t = findEntity(target);
    if (s && t) {
      if (s.entity_id === t.entity_id) throw new Error(`"${alias}" already belongs to "${t.canonical_name}".`);
      return mergeIds(s.entity_id, t.entity_id, btn);
    }
    // legacy free-text rule: the alias is not (yet) a resolved name
    const ok = await confirmDialog({
      title: "Add a name rule?",
      message: `"${alias}" does not match a resolved entity${t ? "" : ` and "${target}" is not an entity name either`}. A case rule will merge that spelling into "${target}" on every re-cluster.`,
      confirmLabel: "Add rule and re-cluster",
    });
    if (!ok || !ctx.isCurrent()) return false;
    const before = countNow();
    const done = await withBusy(btn, async () => {
      await ctx.api.post("entities/recluster", { alias_name: alias, canonical_target: target, similarity_threshold: caseThreshold() });
      await afterMutation();
      return true;
    }, { error: "Merge failed" });
    if (done && ctx.isCurrent()) toast.success(`Merged "${alias}" into "${target}" (${int(before)} → ${int(countNow())} entities).`, { title: "Entities re-clustered" });
    return !!done;
  }

  slot("merge-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const err = slot("merge-err");
    const a = form.elements.alias_name.value.trim();
    const t = form.elements.canonical_target.value.trim();
    let msg = "";
    if (!a || !t) msg = "Enter both the name to absorb and the entity to merge into.";
    else if (a.toUpperCase() === t.toUpperCase()) msg = "The two names are the same.";
    const showErr = (m) => {
      err.hidden = !m;
      err.textContent = m;
    };
    showErr(msg);
    form.elements.alias_name.setAttribute("aria-invalid", msg && (!a || a.toUpperCase() === t.toUpperCase()) ? "true" : "false");
    form.elements.canonical_target.setAttribute("aria-invalid", msg && !t ? "true" : "false");
    if (msg) return;
    try {
      const done = await mergeByName(a, t, form.querySelector('button[type="submit"]'));
      if (done) form.reset();
    } catch (ex) {
      showErr(ex?.message || String(ex));
    }
  });

  delegate(el, "click", '[data-action="near-merge"]', (e, btn) => {
    const c = nearList[Number(btn.dataset.i)];
    if (!c) return;
    if (btn.dataset.dir === "ab") mergeIds(c.entity_b_id, c.entity_a_id, btn);
    else mergeIds(c.entity_a_id, c.entity_b_id, btn);
  });
  delegate(el, "click", '[data-action="near-reject"]', (e, btn) => {
    const c = nearList[Number(btn.dataset.i)];
    if (c) rejectIds(c.entity_a_id, c.entity_b_id, btn);
  });

  async function undo(decisionId, btn) {
    const d = (decisions.decisions || []).find((x) => x.decision_id === decisionId);
    const text = d ? decisionText(d, nameOf) : "the most recent decision";
    const ok = await confirmDialog({ title: "Undo decision?", message: `Undo: ${text}. The entities are restored with their original IDs; the undo is logged.`, confirmLabel: "Undo" });
    if (!ok || !ctx.isCurrent()) return;
    await postDecision("entities/undo", decisionId ? { decision_id: decisionId } : {}, btn,
      (r) => (r.decision ? `Undone: ${decisionText(r.decision, nameOf)}.` : "Nothing to undo."));
  }
  delegate(el, "click", '[data-action="undo"]', (e, btn) => undo(btn.dataset.id, btn));
  delegate(el, "click", '[data-action="undo-last"]', (e, btn) => {
    const list = decisions.decisions || [];
    if (list.length) undo(list[list.length - 1].decision_id, btn);
  });

  // ------------------------------------------------------------------ redraw panels (patch, no full re-render)
  function refreshPanels() {
    drawKpis();
    drawPreview();
    drawNear();
    drawNames();
    drawDecisions();
    grid.setRows(allEntities());
  }
  drawKpis();
  drawPreview();
  drawNear();
  drawNames();
  drawDecisions();
  loadPreview();
  loadDecisions();

  ctx.onData([TOPICS.entities], () => {
    const followCase = Math.abs(Number(range.value) - threshold) < 0.001;
    if (followCase) range.value = String(caseThreshold());
    threshold = caseThreshold();
    refreshPanels();
    if (followCase && preview && Math.abs(Number(preview.threshold) - Number(range.value)) > 0.001) loadPreviewSoon();
  });

  ctx.registerCommands([
    { id: "entities.recluster", title: "Entities: re-cluster at slider threshold…", group: "Entities", icon: "refresh", run: () => el.querySelector('[data-action="recluster"]').click() },
    { id: "entities.merge", title: "Entities: manual merge…", group: "Entities", icon: "merge", run: () => slot("merge-form").elements.alias_name.focus() },
    { id: "entities.undo", title: "Entities: undo last review decision…", group: "Entities", icon: "arrow-left", run: () => el.querySelector('[data-action="undo-last"]').click() },
    { id: "entities.system", title: "Entities: show / hide system entities", group: "Entities", icon: "filter", run: () => grid.setFilters({ system: grid.getFilters().system ? "" : "true" }) },
    { id: "entities.details", title: "Entities: show details-only entities", group: "Entities", icon: "users", run: () => grid.setFilters({ status: "details" }) },
  ]);

  return {
    update(params) {
      grid.setFilters({ q: params.q || "", status: params.status || "", system: params.system || "" }, { replace: true, silent: true });
      if (params.sort) grid.setSort(params.sort, params.order || "asc", { silent: true });
    },
  };
}
