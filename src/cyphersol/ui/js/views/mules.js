/* Mule risk scoring (#/case/{id}/mules) — FR-TRACE-05, report TF-15 / §5.3 item 4.
 *
 *   - ranked, sortable, filterable table of every scored account (GET trace/mules)
 *   - tier tiles (Critical ≥ 80 / Suspicious 50–79 / Low < 50, risk tokens) that filter the table
 *   - explainability panel for the selected account: contribution waterfall (w·S·100 steps summing to the
 *     composite) and, for all 10 SRS parameters (assessment.explainability[]), the raw metric in words, the
 *     normalised score S (0–1), the weight, the points, the formula and the parameter's evidence rows
 *   - evidence drawer (whole account or one parameter) resolving the exact tx_ids (≤ 500 per call), with
 *     links into Transactions using the exact `ids` filter (sent to the server as tx_ids), chunked at 500
 *
 * URL params: entity (selected account's entity_id), q (search), tier (Critical|Suspicious|Low Risk), sort, order.
 */
import { RISK_TIERS, tierClass } from "../core/constants.js";
import { announce, delegate, html, render } from "../core/dom.js";
import { DASH, date, hours, inr, inrCompact, int, num, pct, plural } from "../core/format.js";
import { isAbort } from "../core/api.js";
import { TOPICS } from "../core/store.js";
import { contributionWaterfall } from "../charts/bars.js";
import { createDataGrid } from "../components/datagrid.js";
import { openDrawer } from "../components/drawer.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { toast } from "../components/toast.js";
import { contributionSum, evidenceLinks, explainRows, scoreTitle } from "./mules-explain.js";
import { channelLabel, channelOf, flagInfo, idChunks } from "./transactions-meta.js";

export const styles = ["views/risk.css"];

/** What a high S (0–1) means for each parameter (the raw metric itself comes from explainability[].raw). */
const SIGNALS = {
  flow_through_ratio: "Share of inflow paid straight back out (debits ÷ credits).",
  velocity_hold_time: "How quickly credited funds leave the account.",
  lifestyle_spending_absence: "No everyday spending (groceries, utilities, fuel, rent).",
  counterparty_concentration: "Herfindahl concentration of counterparties.",
  fan_in_fan_out_pattern: "Many senders feeding few beneficiaries in short windows.",
  cash_crypto_wallet_leakage: "Outflow exiting to cash, crypto / VDA exchanges or wallets.",
  structuring_smurfing: "Amounts kept just under reporting thresholds.",
  zero_balance_oscillation: "Balance repeatedly drained to near zero.",
  burst_dormancy_pattern: "Bursts of activity after dormant periods.",
  round_number_anomalies: "Share of round-figure amounts.",
};

const TIER_ORDER = ["Critical", "Suspicious", "Low Risk"];

function prepare(list) {
  const seen = new Set();
  return (list || [])
    .slice()
    .sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0))
    .map((a, i) => {
      let key = String(a.account_number || a.entity_id || `row${i}`);
      if (seen.has(key)) key = `${key}#${i}`;
      seen.add(key);
      const explain = explainRows(a);
      let top = null;
      for (const r of explain) {
        const v = Number(r.contribution) || 0;
        if (!top || v > top.v) top = { p: r, v };
      }
      // the waterfall reads weighted_contributions / parameters: feed it the explainability figures
      const weighted = Object.fromEntries(explain.map((r) => [r.key, r.contribution ?? 0]));
      const params = Object.fromEntries(explain.map((r) => [r.key, r.score ?? 0]));
      return { ...a, weighted_contributions: weighted, parameters: params, _explain: explain, _key: key, _rank: i + 1, _top: top, _evidence: new Set(a.flagged_evidence_tx_ids || []).size };
    });
}

const tierBadge = (tier) => html`<span class="badge badge--${tierClass(tier)}">${RISK_TIERS[tier]?.label || tier || "Unscored"}</span>`;

const scoreBar = (score, tier) => html`<span class="v-mules-score">
  <svg class="v-mules-score__bar" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true" focusable="false">
    <rect class="v-risk-bars__bg" x="0" y="0" width="100" height="6" rx="1"></rect>
    <rect class="fill-${tierClass(tier)}" x="0" y="0" width="${Math.max(0.5, Math.min(100, Number(score) || 0))}" height="6" rx="1"></rect>
  </svg><span class="num tabular strong">${num(score, 1)}</span></span>`;

export async function mount(el, ctx) {
  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Mule risk scoring",
        sub: html`10-parameter behavioural model. Tiers: <span class="badge badge--critical">Critical ≥ 80</span>
          <span class="badge badge--suspicious">Suspicious 50–79</span> <span class="badge badge--low">Low &lt; 50</span>`,
        actions: html`<button type="button" class="btn btn--sm" data-action="reload">${icon("refresh", { size: "sm" })}Refresh</button>`,
      })}
      <div data-slot="kpis">${skeleton({ kind: "kpis", rows: 5 })}</div>
      <section class="card" aria-labelledby="mu-rank">
        <div class="card__head"><div><h2 class="card__title" id="mu-rank">Ranked accounts</h2>
          <p class="card__sub">Select an account to see why it scored. Scores are recomputed from the current case data.</p></div></div>
        <div class="card__body card__body--flush" data-slot="grid"></div>
      </section>
      <section class="card v-mules-detail" aria-labelledby="mu-detail" data-slot="detail" hidden></section>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);

  let rows = [];
  let selectedKey = null;
  let drawer = null;
  let drawerCtl = null;
  ctx.onCleanup(() => {
    drawerCtl?.abort();
    drawer?.close();
  });

  const findByEntity = (id) => rows.find((r) => r.entity_id === id) || null;
  const selected = () => rows.find((r) => r._key === selectedKey) || null;

  // ------------------------------------------------------------------ KPIs (tier tiles filter the table)
  const drawKpis = () => {
    const by = { Critical: 0, Suspicious: 0, "Low Risk": 0 };
    rows.forEach((r) => (by[r.risk_tier] = (by[r.risk_tier] || 0) + 1));
    const top = rows[0];
    const exposure = rows.filter((r) => r.risk_tier !== "Low Risk").reduce((s, r) => s + (r.total_credited || 0), 0);
    render(
      slot("kpis"),
      kpiGrid(
        [
          { label: "Accounts scored", value: int(rows.length), icon: "users", href: ctx.href("mules") },
          { label: "Critical (≥ 80)", value: int(by.Critical), tone: by.Critical ? "critical" : "success", icon: "alert", href: ctx.href("mules", { tier: "Critical" }) },
          { label: "Suspicious (50–79)", value: int(by.Suspicious), tone: by.Suspicious ? "warn" : "", icon: "shield", href: ctx.href("mules", { tier: "Suspicious" }) },
          { label: "Low risk (< 50)", value: int(by["Low Risk"]), href: ctx.href("mules", { tier: "Low Risk" }) },
          { label: "Highest score", value: top ? num(top.composite_score, 1) : DASH, sub: top ? top.account_holder_name : "", tone: top ? (tierClass(top.risk_tier) === "critical" ? "critical" : tierClass(top.risk_tier) === "suspicious" ? "warn" : "") : "" },
          { label: "Flagged inflow", value: inrCompact(exposure), title: inr(exposure), sub: "Critical + Suspicious", icon: "arrow-down-left" },
        ],
        { label: "Mule scoring summary" }
      )
    );
  };

  // ------------------------------------------------------------------ ranked grid (client mode)
  const grid = createDataGrid(slot("grid"), {
    ariaLabel: "Mule risk ranking",
    rowKey: "_key",
    rows: [],
    rowHeight: 50,
    height: "min(60vh, 560px)",
    countNoun: ["account", "accounts"],
    initialSort: { key: ctx.params.sort || "composite_score", order: ctx.params.order || "desc" },
    initialFilters: { q: ctx.params.q || "", tier: ctx.params.tier || "" },
    filters: [
      { key: "q", type: "search", label: "Search accounts", placeholder: "Holder, bank or account…" },
      { key: "tier", type: "select", label: "Risk tier", options: [{ value: "", label: "All tiers" }, ...TIER_ORDER.map((t) => ({ value: t, label: RISK_TIERS[t].label }))] },
    ],
    clientFilter: (r, f) => {
      if (f.tier && r.risk_tier !== f.tier) return false;
      if (f.q) {
        const q = String(f.q).toLowerCase();
        return [r.account_holder_name, r.bank_name, r.account_number].some((v) => String(v || "").toLowerCase().includes(q));
      }
      return true;
    },
    onStateChange: ({ sort, order, filters }) =>
      ctx.setParams({ q: filters.q || null, tier: filters.tier || null, sort: sort === "composite_score" ? null : sort, order: sort === "composite_score" && order === "desc" ? null : order }),
    onRowClick: (r) => select(r._key, { push: true, scroll: true }),
    emptyState: { icon: "shield", title: "No accounts scored", message: "Mule scoring needs at least one ingested statement.", action: { label: "Upload statements", href: ctx.href("ingest") } },
    columns: [
      { key: "_rank", label: "#", width: "48px", align: "right", sortable: true, sortValue: (r) => r._rank, className: (r) => sel(r, "num tabular muted") },
      { key: "account_holder_name", label: "Account holder", width: "minmax(210px, 2fr)", sortable: true, className: (r) => sel(r),
        render: (r) => html`<span class="strong truncate">${r.account_holder_name || DASH}</span><span class="dg__sub">${r.bank_name || "Unknown bank"} · <span class="mono">${r.account_number || DASH}</span></span>` },
      { key: "composite_score", label: "Score", width: "150px", sortable: true, defaultOrder: "desc", className: (r) => sel(r),
        title: (r) => `Composite ${num(r.composite_score, 2)} / 100`, render: (r) => scoreBar(r.composite_score, r.risk_tier) },
      { key: "risk_tier", label: "Tier", width: "112px", sortable: true, defaultOrder: "desc", sortValue: (r) => r.composite_score, className: (r) => sel(r), render: (r) => tierBadge(r.risk_tier) },
      { key: "_top", label: "Top driver", width: "minmax(170px, 1.3fr)", sortable: true, sortValue: (r) => r._top?.v || 0, defaultOrder: "desc", className: (r) => sel(r),
        render: (r) => (r._top && r._top.v > 0 ? html`<span class="truncate">${r._top.p.label}</span><span class="dg__sub tabular">+${num(r._top.v, 1)} of ${num(r._top.p.max, 0)} pts</span>` : DASH) },
      { key: "total_credited", label: "Credited", width: "120px", align: "right", sortable: true, defaultOrder: "desc", className: (r) => sel(r, "is-credit"), title: (r) => inr(r.total_credited), format: (v) => inrCompact(v) },
      { key: "total_debited", label: "Debited", width: "120px", align: "right", sortable: true, defaultOrder: "desc", className: (r) => sel(r, "is-debit"), title: (r) => inr(r.total_debited), format: (v) => inrCompact(v) },
      { key: "median_hold_hours", label: "Hold", width: "80px", align: "right", sortable: true, className: (r) => sel(r, "tabular"), title: () => "Median hold time of credited funds", format: (v) => hours(v) },
      { key: "cash_crypto_leakage_pct", label: "Leakage", width: "88px", align: "right", sortable: true, defaultOrder: "desc", className: (r) => sel(r, "tabular"), title: () => "Share of outflow to cash / crypto / wallets", format: (v) => pct(v, { ratio: false }) },
      { key: "tx_count", label: "Txns", width: "68px", align: "right", sortable: true, defaultOrder: "desc", className: (r) => sel(r, "tabular"), format: (v) => int(v) },
      { key: "_evidence", label: "Evidence", width: "112px", sortable: true, defaultOrder: "desc", className: (r) => sel(r),
        render: (r) => (r._evidence
          ? html`<button type="button" class="btn btn--sm btn--subtle" data-action="evidence" aria-label="Open ${r._evidence} evidence rows for ${r.account_holder_name}">${icon("eye", { size: "sm" })}${int(r._evidence)}</button>`
          : html`<span class="muted small">None</span>`) },
    ],
  });
  ctx.onCleanup(() => grid.destroy());
  function sel(r, extra = "") {
    return `${extra} ${r._key === selectedKey ? "v-mules-sel" : ""}`.trim();
  }
  delegate(grid.el, "click", '[data-action="evidence"]', (e) => {
    const r = grid.rowFromEvent(e);
    if (r) openEvidence(r);
  });

  // ------------------------------------------------------------------ selection + explainability panel
  function select(key, { push = false, scroll = false, updateUrl = true } = {}) {
    const prev = selectedKey;
    selectedKey = key;
    const r = selected();
    if (prev && prev !== key) grid.updateRow(prev, (x) => x);
    if (r) grid.updateRow(key, (x) => x);
    drawDetail();
    if (r && prev !== key) announce(`Score breakdown shown for ${r.account_holder_name || r.account_number}.`);
    if (updateUrl) ctx.setParams({ entity: r?.entity_id || null }, { push });
    ctx.setCrumbs?.(r ? [{ label: r.account_holder_name || r.account_number }] : []);
    if (scroll && r) slot("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /** Links into Transactions with the exact evidence ids (chunks of ≤ 500, the server's tx_ids cap). */
  const evidenceAnchors = (r, a) => {
    const links = evidenceLinks(r.evidenceIds);
    if (!links.length) return html`<span class="muted small">${r.evidenceCount ? `${int(r.evidenceCount)} (not listed)` : "None"}</span>`;
    return html`<span class="v-mules-evlinks">${links.map((l, i) => {
      const n = l.ids.split(",").length;
      const lbl = links.length > 1 ? `${int(i * 500 + 1)}–${int(i * 500 + n)}` : int(n);
      return html`<a class="btn btn--ghost btn--sm" href="${ctx.href("transactions", { ids: l.ids })}"
        aria-label="Open ${plural(n, "evidence row")} for ${r.label} (${a.account_holder_name || a.account_number}) in Transactions" title="Open these exact rows in Transactions">${icon("filter", { size: "sm" })}${lbl}</a>`;
    })}<button type="button" class="btn btn--ghost btn--sm btn--icon" data-action="param-evidence" data-key="${r.key}" aria-label="Preview ${r.label} evidence rows" title="Preview rows here">${icon("eye", { size: "sm" })}</button></span>`;
  };

  function paramRows(a) {
    return a._explain.map((r) => {
      const s = r.score;
      const lvl = s == null ? "" : s >= 0.8 ? "critical" : s >= 0.5 ? "suspicious" : "";
      return html`<tr>
        <th scope="row"><span class="strong">${r.label}</span>
          <span class="v-mules-sig">${SIGNALS[r.key] || ""}</span>
          ${r.rawText ? html`<span class="v-mules-raw">${r.rawText}</span>` : ""}
          ${r.formula ? html`<span class="v-mules-formula" title="Scoring formula">${r.formula}</span>` : ""}</th>
        <td class="num tabular" title="${r.rawText || "Raw metric"}">${r.rawFig}</td>
        <td class="num tabular" title="${scoreTitle(r)}">${lvl ? html`<span class="badge badge--${lvl}">${num(s, 2)}</span>` : s == null ? DASH : num(s, 2)}</td>
        <td class="num tabular">${num(r.max, 0)}%</td>
        <td class="num tabular strong" title="${scoreTitle(r)}">${r.contribution == null ? DASH : num(r.contribution, 2)}</td>
        <td>${evidenceAnchors(r, a)}</td>
      </tr>`;
    });
  }

  function drawDetail() {
    const box = slot("detail");
    const a = selected();
    if (!a) {
      box.hidden = true;
      render(box, "");
      return;
    }
    box.hidden = false;
    const sum = contributionSum(a._explain);
    render(
      box,
      html`<div class="card__head v-mules-detail__head">
        <div class="min-w-0">
          <h2 class="card__title" id="mu-detail">${a.account_holder_name || a.account_number}</h2>
          <p class="card__sub">${a.bank_name || "Unknown bank"} · <span class="mono">${a.account_number || DASH}</span> · rank ${int(a._rank)} of ${int(rows.length)}</p>
        </div>
        <div class="v-mules-headline">
          <span class="v-mules-big tabular" data-tier="${tierClass(a.risk_tier)}">${num(a.composite_score, 1)}<span class="v-mules-big__of">/100</span></span>
          ${tierBadge(a.risk_tier)}
        </div>
      </div>
      <div class="card__body stack">
        <div class="row-wrap">
          <button type="button" class="btn btn--primary btn--sm" data-action="evidence" ${a._evidence ? "" : html`disabled`}>${icon("eye", { size: "sm" })}Evidence (${int(a._evidence)})</button>
          ${a.entity_id ? html`<a class="btn btn--sm" href="${ctx.href("profile", { entity: a.entity_id })}">${icon("user", { size: "sm" })}Financial profile</a>
            <a class="btn btn--sm" href="${ctx.href("network", { entity: a.entity_id })}">${icon("network", { size: "sm" })}Network</a>
            <a class="btn btn--sm" href="${ctx.href("layering", { source: a.entity_id })}">${icon("layers", { size: "sm" })}Layering</a>` : ""}
        </div>
        <dl class="v-mules-facts">
          <div><dt>Credited</dt><dd class="num tabular is-credit" title="${inr(a.total_credited)}">${inrCompact(a.total_credited)}</dd></div>
          <div><dt>Debited</dt><dd class="num tabular is-debit" title="${inr(a.total_debited)}">${inrCompact(a.total_debited)}</dd></div>
          <div><dt>Transactions</dt><dd class="num tabular">${int(a.tx_count)}</dd></div>
          <div><dt>Median hold</dt><dd class="num tabular">${hours(a.median_hold_hours)}</dd></div>
          <div><dt>Cash / crypto leakage</dt><dd class="num tabular">${pct(a.cash_crypto_leakage_pct, { ratio: false })}</dd></div>
          <div><dt>Lifestyle txns</dt><dd class="num tabular">${int(a.lifestyle_txn_count)}</dd></div>
        </dl>
        <div class="v-mules-explain">
          <div class="stack-sm min-w-0">
            <h3 class="subhead">Contribution waterfall</h3>
            <p class="xsmall muted">Each step is weight × S × 100; the steps add up to the composite score${Math.abs(sum - (a.composite_score || 0)) > 0.2 ? html` (Σ ${num(sum, 1)}; difference is server rounding)` : ""}. Faint bars show each parameter's maximum.</p>
            ${contributionWaterfall(a)}
          </div>
          <div class="stack-sm min-w-0">
            <h3 class="subhead">Parameters</h3>
            <div class="table-wrap"><table class="table table--compact v-mules-params">
              <caption class="sr-only">All 10 mule parameters for ${a.account_holder_name}: raw metric, normalised score S (0–1), weight, points (weight × S × 100) and evidence rows</caption>
              <thead><tr><th scope="col">Parameter, signal &amp; raw metric</th><th scope="col" class="num">Raw</th><th scope="col" class="num">S (0–1)</th><th scope="col" class="num">Weight</th><th scope="col" class="num">Points</th><th scope="col">Evidence</th></tr></thead>
              <tbody>${paramRows(a)}</tbody>
              <tfoot><tr><th scope="row">Composite</th><td></td><td></td><td class="num tabular">100%</td><td class="num tabular strong">${num(a.composite_score, 2)}</td><td></td></tr></tfoot>
            </table></div>
          </div>
        </div>
      </div>`
    );
  }
  delegate(el, "click", '.v-mules-detail [data-action="evidence"]', () => {
    const a = selected();
    if (a) openEvidence(a);
  });
  delegate(el, "click", '.v-mules-detail [data-action="param-evidence"]', (e, btn) => {
    const a = selected();
    const r = a?._explain.find((x) => x.key === btn.dataset.key);
    if (a && r) openEvidence(a, { ids: r.evidenceIds, param: r });
  });

  // ------------------------------------------------------------------ evidence drawer
  /** Resolve flagged_evidence_tx_ids to rows exactly with the server's tx_ids filter (≤ 500 ids per call). */
  async function fetchEvidence(idsIn, signal) {
    const ids = Array.from(new Set(idsIn || []));
    const found = new Map();
    for (const chunk of idChunks(ids)) {
      const res = await ctx.api.get("transactions", { tx_ids: chunk, offset: 0, limit: 500, include_ignored: "true" }, { signal });
      for (const t of res.transactions || []) found.set(t.tx_id, t);
    }
    const rowsOut = Array.from(found.values()).sort((x, y) => String(x.txn_date).localeCompare(String(y.txn_date)) || (x.row_index || 0) - (y.row_index || 0));
    const missing = ids.filter((id) => !found.has(id));
    return { rows: rowsOut, missing };
  }

  function evidenceBody(a, { rows: ev, missing }, param = null) {
    const cr = ev.reduce((s, t) => s + (t.credit || 0), 0);
    const dr = ev.reduce((s, t) => s + (t.debit || 0), 0);
    const dates = ev.map((t) => t.txn_date).filter(Boolean).sort();
    const links = evidenceLinks(ev.map((t) => t.tx_id));
    const allHref = ctx.href("transactions", { q: a.account_holder_name || null, sort: "txn_date" });
    return html`<div class="stack">
      <div class="row-wrap">${tierBadge(a.risk_tier)}<span class="tabular strong">${num(a.composite_score, 1)} / 100</span>
        <span class="muted small">${a.bank_name || ""} · <span class="mono">${a.account_number || DASH}</span></span></div>
      ${param
        ? html`<p class="small text-2"><strong>${param.label}</strong>: the rows behind this parameter's score (S ${param.score == null ? DASH : num(param.score, 2)}, ${num(param.contribution, 2)} of ${num(param.max, 0)} points).${param.rawText ? html` ${param.rawText}.` : ""}</p>`
        : html`<p class="small text-2">Rows the scorer flagged as evidence for this account's score (rapid pass-through, threshold-hugging credits, cash/crypto exits…).</p>`}
      <dl class="v-mules-facts">
        <div><dt>Flagged rows</dt><dd class="num tabular">${int(ev.length)}${missing.length ? html` <span class="muted">of ${int(ev.length + missing.length)}</span>` : ""}</dd></div>
        <div><dt>Credits</dt><dd class="num tabular is-credit" title="${inr(cr)}">${inrCompact(cr)}</dd></div>
        <div><dt>Debits</dt><dd class="num tabular is-debit" title="${inr(dr)}">${inrCompact(dr)}</dd></div>
        <div><dt>Window</dt><dd class="tabular">${dates.length ? html`${date(dates[0])} → ${date(dates[dates.length - 1])}` : DASH}</dd></div>
      </dl>
      ${ev.length
        ? html`<ol class="v-mules-ev" aria-label="Evidence transactions">${ev.map((t) => {
            const cr0 = (t.credit || 0) > 0;
            const flags = (t.anomaly_flags || []).filter((f) => f !== "VALID");
            return html`<li class="v-mules-ev__row">
              <div class="v-mules-ev__main min-w-0">
                <div class="row-between"><span class="tabular small text-2">${date(t.txn_date)} · ${channelLabel(channelOf(t))}</span>
                  <span class="num tabular strong ${cr0 ? "is-credit" : "is-debit"}">${cr0 ? `+${inr(t.credit)}` : inr(-(t.debit || 0))}</span></div>
                <div class="v-mules-ev__narr">${t.raw_narration}</div>
                <div class="row-wrap xsmall">
                  <span class="muted">${cr0 ? "From" : "To"}: <span class="text-2">${t.extracted_counterparty || "Unknown"}</span></span>
                  <span class="badge ${t.is_suspense ? "badge--suspicious" : "badge--neutral"}">${t.category_name || "Uncategorised"}</span>
                  ${flags.map((f) => html`<span class="badge badge--${flagInfo(f).tone}">${flagInfo(f).label}</span>`)}
                  ${t.is_duplicate_ignored ? html`<span class="badge badge--outline">Ignored duplicate</span>` : ""}
                </div>
              </div>
              <a class="btn btn--ghost btn--sm btn--icon" href="${ctx.href("transactions", { ids: t.tx_id })}"
                aria-label="Open this ${cr0 ? "credit" : "debit"} of ${inr(cr0 ? t.credit : t.debit)} on ${date(t.txn_date)} in Transactions" title="Open in Transactions">${icon("arrow-right", { size: "sm" })}</a>
            </li>`;
          })}</ol>`
        : emptyState({ icon: "search", title: "Evidence rows not found", message: "The flagged rows could not be located in the current case data (the case may have changed since scoring).", compact: true })}
      ${missing.length ? html`<details class="v-mules-missing"><summary class="small">${plural(missing.length, "transaction ID")} not found</summary>
          <ul class="mono xsmall">${missing.map((id) => html`<li>${id}</li>`)}</ul></details>` : ""}
      <div class="row-wrap">
        ${links.map((l, i) => html`<a class="btn btn--sm ${i ? "" : "btn--primary"}" href="${ctx.href("transactions", { ids: l.ids })}">${icon("filter", { size: "sm" })}Open ${links.length > 1 ? `rows ${int(i * 500 + 1)}–${int(i * 500 + l.ids.split(",").length)}` : "evidence rows"} in Transactions</a>`)}
        <a class="btn btn--sm" href="${allHref}">${icon("list", { size: "sm" })}Open account in Transactions</a>
        <button type="button" class="btn btn--sm btn--ghost" data-action="copy-ids">${icon("hash", { size: "sm" })}Copy transaction IDs</button>
      </div>
    </div>`;
  }

  async function openEvidence(a, { ids = null, param = null } = {}) {
    const evIds = Array.from(new Set(ids || a.flagged_evidence_tx_ids || []));
    drawerCtl?.abort();
    const ctl = new AbortController();
    drawerCtl = ctl;
    const onViewAbort = () => ctl.abort();
    ctx.signal.addEventListener("abort", onViewAbort, { once: true });
    drawer = openDrawer({
      title: html`Evidence — ${a.account_holder_name || a.account_number}${param ? html` · ${param.label}` : ""}`,
      width: 600,
      className: "v-mules-drawer",
      body: skeleton({ kind: "text", rows: 8 }),
      onClose: () => {
        ctl.abort();
        ctx.signal.removeEventListener("abort", onViewAbort);
      },
    });
    const d = drawer;
    d.body.addEventListener("click", async (e) => {
      if (e.target.closest('[data-action="copy-ids"]')) {
        try {
          await navigator.clipboard.writeText(evIds.join("\n"));
          toast.success(`${plural(evIds.length, "transaction ID")} copied`);
        } catch (_err) {
          toast.warn("Clipboard is not available in this browser context.");
        }
      } else if (e.target.closest('[data-action="ev-retry"]')) {
        load();
      } else if (e.target.closest("a[href]")) {
        d.close();
      }
    });
    const load = async () => {
      d.setBody(skeleton({ kind: "text", rows: 8 }));
      try {
        const res = await fetchEvidence(evIds, ctl.signal);
        if (ctl.signal.aborted) return;
        d.setBody(evidenceBody(a, res, param));
      } catch (err) {
        if (isAbort(err) || ctl.signal.aborted) return;
        d.setBody(errorState(err, { retryId: "ev-retry", title: "Could not load evidence", compact: true }));
      }
    };
    load();
  }

  // ------------------------------------------------------------------ data
  async function load({ quiet = false } = {}) {
    if (!quiet) render(slot("kpis"), skeleton({ kind: "kpis", rows: 5 }));
    let res;
    try {
      res = await ctx.api.get("trace/mules");
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      render(slot("kpis"), errorState(err, { retryId: "reload", title: "Could not load mule scores" }));
      grid.setRows([]);
      return;
    }
    if (!ctx.isCurrent()) return;
    rows = prepare(res.assessments);
    drawKpis();
    grid.setRows(rows);
    const want = ctx.params.entity ? findByEntity(ctx.params.entity) : null;
    const keep = rows.find((r) => r._key === selectedKey);
    const pick = want || keep || rows[0] || null;
    select(pick ? pick._key : null, { updateUrl: Boolean(ctx.params.entity && !want) });
  }

  delegate(el, "click", '[data-action="reload"]', (e, btn) => {
    if (btn.closest(".v-mules-detail")) return;
    ctx.withBusy ? ctx.withBusy(btn, () => load({ quiet: true }), { silent: true }) : load({ quiet: true });
  });

  await load();
  if (!ctx.isCurrent()) return undefined;

  ctx.onData([TOPICS.analytics], () => load({ quiet: true }));

  ctx.registerCommands([
    { id: "mules.critical", title: "Mules: show Critical accounts only", group: "Mule scoring", icon: "alert", keywords: ["tier", "filter", "risk"], run: () => grid.setFilters({ tier: "Critical" }) },
    { id: "mules.suspicious", title: "Mules: show Suspicious accounts", group: "Mule scoring", icon: "shield", keywords: ["tier", "filter"], run: () => grid.setFilters({ tier: "Suspicious" }) },
    { id: "mules.all", title: "Mules: show all tiers", group: "Mule scoring", icon: "filter", run: () => grid.setFilters({ tier: "", q: "" }) },
    { id: "mules.evidence", title: "Mules: open evidence for selected account", group: "Mule scoring", icon: "eye", when: () => Boolean(selected()?._evidence), run: () => openEvidence(selected()) },
    { id: "mules.profile", title: "Mules: open profile of selected account", group: "Mule scoring", icon: "user", when: () => Boolean(selected()?.entity_id), run: () => ctx.navigate("profile", { entity: selected().entity_id }) },
    { id: "mules.top", title: "Mules: select highest-scoring account", group: "Mule scoring", icon: "zap", when: () => rows.length > 0, run: () => select(rows[0]._key, { push: true, scroll: true }) },
  ]);

  return {
    update(params) {
      grid.setFilters({ q: params.q || "", tier: params.tier || "" }, { replace: true, silent: true });
      if (params.sort || params.order) grid.setSort(params.sort || "composite_score", params.order || "desc", { silent: true });
      const want = params.entity ? findByEntity(params.entity) : null;
      if (want && want._key !== selectedKey) select(want._key, { updateUrl: false });
    },
  };
}
