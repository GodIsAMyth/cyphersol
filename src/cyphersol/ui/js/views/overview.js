/* Case overview (#/case/{id}): KPIs, statements with custody hashes, largest transactions, audit trail.
 * Also the REFERENCE IMPLEMENTATION of the view contract for the other view agents:
 *   - reads cached case data from the store (store.ensureCaseData / select)
 *   - case-scoped requests through ctx.api with the view's AbortSignal
 *   - createDataGrid in client mode (statements, audit) and server mode (largest transactions)
 *   - ctx.onData() to refresh after mutations elsewhere, ctx.registerCommands() for palette entries
 *   - returns a cleanup function. */
import { caseModeLabel, flagMeta } from "../core/constants.js";
import { html, render } from "../core/dom.js";
import { date, dateTime, inr, inrCompact, int } from "../core/format.js";
import { describeAgentTelemetry } from "../core/results.js";
import { TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { pageHead } from "../components/states.js";

export async function mount(el, ctx) {
  const data = await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return;

  render(
    el,
    html`<div class="stack-lg">
      <div data-slot="head"></div>
      <div data-slot="kpis"></div>
      <div data-slot="telemetry"></div>
      <section class="card" aria-labelledby="ov-stmts">
        <div class="card__head"><div><h2 class="card__title" id="ov-stmts">Statements &amp; chain of custody</h2>
          <p class="card__sub">SHA-256 of each source file as ingested.</p></div>
          <a class="btn btn--sm" href="${ctx.href("ingest")}">${icon("upload", { size: "sm" })}Upload statements</a></div>
        <div class="card__body" data-slot="statements"></div>
      </section>
      <section class="card" aria-labelledby="ov-largest">
        <div class="card__head"><div><h2 class="card__title" id="ov-largest">Largest transactions</h2>
          <p class="card__sub">Sorted by amount on the server; open Transactions for full filtering.</p></div>
          <a class="btn btn--sm" href="${ctx.href("transactions", { sort: "amount", order: "desc" })}">Open in Transactions${icon("arrow-right", { size: "sm" })}</a></div>
        <div class="card__body" data-slot="largest"></div>
      </section>
      <section class="card" aria-labelledby="ov-audit">
        <div class="card__head"><div><h2 class="card__title" id="ov-audit">Audit trail</h2>
          <p class="card__sub">Immutable operator log (latest 50 entries).</p></div></div>
        <div class="card__body" data-slot="audit"></div>
      </section>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);

  const drawHeader = (d) => {
    const c = d.case || ctx.case || {};
    render(
      slot("head"),
      pageHead({
        title: c.title || "Case",
        sub: html`<span class="badge ${c.mode === "cypher_edge" ? "badge--info" : "badge--accent"}">${caseModeLabel(c.mode)}</span>
          ${c.agency_or_firm_name ? html` · ${c.agency_or_firm_name}` : ""} · created ${date(c.created_at)} ·
          <span class="mono xsmall">${c.case_id}</span>`,
      })
    );
  };

  const drawKpis = (d) => {
    const s = d.summary || {};
    const net = (s.total_credits || 0) - (s.total_debits || 0);
    render(
      slot("kpis"),
      kpiGrid(
        [
          { label: "Statements", value: int(s.statements_count), icon: "file", href: ctx.href("ingest") },
          { label: "Transactions", value: int(s.transactions_count), icon: "list", href: ctx.href("transactions") },
          { label: "Total credits", value: inrCompact(s.total_credits), title: inr(s.total_credits), icon: "arrow-down-left" },
          { label: "Total debits", value: inrCompact(s.total_debits), title: inr(s.total_debits), icon: "arrow-up-right" },
          { label: "Net flow", value: inrCompact(net, { sign: true }), title: inr(net), tone: net < 0 ? "danger" : "" },
          { label: "Entities", value: int(s.entities_count), icon: "users", href: ctx.href("entities") },
          { label: "Suspense pending", value: int(s.suspense_count), tone: s.suspense_count ? "warn" : "success", href: ctx.href("suspense") },
          { label: "Flagged rows", value: int(s.anomaly_count), tone: s.anomaly_count ? "danger" : "success", href: ctx.href("anomalies") },
        ],
        { label: "Case summary" }
      )
    );
  };

  const drawTelemetry = (d) => {
    const t = describeAgentTelemetry(d.latest_agent_telemetry);
    render(
      slot("telemetry"),
      t
        ? html`<div class="callout callout--info">${icon("cpu")}<div><strong>Last pipeline run (${t.mode})</strong> — ${t.summary}
            ${t.warnings.length ? html`<div class="xsmall">Notes: ${t.warnings.join(" | ")}</div>` : ""}</div></div>`
        : ""
    );
  };

  // --- statements (client-mode grid)
  const stmtGrid = createDataGrid(slot("statements"), {
    ariaLabel: "Ingested statements",
    rowKey: "statement_id",
    rows: data.statements || [],
    height: "auto",
    rowHeight: 52,
    showToolbar: false,
    countNoun: ["statement", "statements"],
    initialSort: { key: "period_start", order: "asc" },
    emptyState: { icon: "upload", title: "No statements yet", message: "Upload PDF, Excel, CSV or image statements to begin.", action: { label: "Upload statements", href: ctx.href("ingest") } },
    columns: [
      { key: "source_filename", label: "File & period", width: "minmax(220px, 2fr)", sortable: true,
        render: (s) => html`<span class="strong">${s.source_filename}</span><span class="dg__sub">${date(s.period_start)} → ${date(s.period_end)}</span>` },
      { key: "bank_name", label: "Bank & account", width: "minmax(180px, 1.4fr)", sortable: true,
        render: (s) => html`${s.bank_name}<span class="dg__sub mono">${s.account_number}${s.ifsc_code ? ` · ${s.ifsc_code}` : ""}</span>` },
      { key: "account_holder_name", label: "Holder", width: "minmax(160px, 1.2fr)", sortable: true },
      { key: "total_transactions", label: "Txns", width: "80px", align: "right", sortable: true, format: (v) => int(v) },
      { key: "balance_mismatch_count", label: "Quality", width: "170px",
        render: (s) => html`${s.balance_mismatch_count
          ? html`<span class="badge badge--danger">${int(s.balance_mismatch_count)} mismatch</span>`
          : html`<span class="badge badge--success">Balanced</span>`}
          ${s.duplicate_suspect_count ? html` <span class="badge badge--warn">${int(s.duplicate_suspect_count)} dup</span>` : ""}
          ${s.is_ocr_derived ? html` <span class="badge badge--info">OCR</span>` : ""}` },
      { key: "sha256_hash", label: "SHA-256 (custody)", width: "minmax(300px, 2fr)", wrap: true,
        title: (s) => s.sha256_hash, render: (s) => html`<span class="hash">${s.sha256_hash}</span>` },
    ],
  });
  ctx.onCleanup(() => stmtGrid.destroy());

  // --- largest transactions (server-mode grid using sort=amount&order=desc)
  const bigGrid = createDataGrid(slot("largest"), {
    ariaLabel: "Largest transactions",
    rowKey: "tx_id",
    height: "360px",
    pageSize: 100,
    showToolbar: true,
    countNoun: ["transaction", "transactions"],
    initialSort: { key: "amount", order: "desc" },
    filters: [{ key: "direction", type: "select", label: "Direction", options: [{ value: "", label: "Credits & debits" }, { value: "CR", label: "Credits" }, { value: "DR", label: "Debits" }] }],
    fetchPage: ({ offset, limit, sort, order, filters, signal }) =>
      ctx.api.get("transactions", { offset, limit, sort, order, include_ignored: "false", ...filters }, { signal }).then((r) => ({ rows: r.transactions, total: r.total })),
    onRowClick: (t) => ctx.navigate("transactions", { q: t.utr_number || t.raw_narration.slice(0, 40) }),
    emptyState: { icon: "list", title: "No transactions yet" },
    columns: [
      { key: "txn_date", label: "Date", width: "110px", sortable: true, sortKey: "txn_date", format: (v) => date(v) },
      { key: "raw_narration", label: "Narration & counterparty", width: "minmax(260px, 3fr)", sortable: true,
        render: (t) => html`${t.raw_narration}<span class="dg__sub">${t.extracted_counterparty || "—"} · ${t.payment_channel}</span>` },
      { key: "source_holder_name", label: "Account", width: "minmax(150px, 1fr)", sortable: true,
        render: (t) => html`${t.source_holder_name || "—"}<span class="dg__sub mono">${t.source_account_number || ""}</span>` },
      { key: "amount", label: "Amount", width: "150px", align: "right", sortable: true, defaultOrder: "desc",
        className: (t) => (t.credit > 0 ? "is-credit" : "is-debit"),
        title: (t) => (t.credit > 0 ? `Credit ${inr(t.credit)}` : `Debit ${inr(t.debit)}`),
        render: (t) => (t.credit > 0 ? html`+${inr(t.credit)}` : html`${inr(-t.debit)}`) },
      { key: "category_name", label: "Category", width: "minmax(160px, 1fr)", sortable: true,
        render: (t) => html`<span class="badge ${t.is_suspense ? "badge--suspicious" : "badge--neutral"}">${t.category_name}</span>
          ${(t.anomaly_flags || []).filter((f) => f !== "VALID").slice(0, 1).map((f) => html` <span class="badge badge--${flagMeta(f).tone}">${flagMeta(f).label}</span>`)}` },
    ],
  });
  ctx.onCleanup(() => bigGrid.destroy());

  // --- audit trail (client grid)
  const auditGrid = createDataGrid(slot("audit"), {
    ariaLabel: "Audit trail",
    rows: data.audit_logs || [],
    height: "320px",
    showToolbar: false,
    countNoun: ["entry", "entries"],
    emptyState: { icon: "file", title: "No audit entries" },
    columns: [
      { key: "created_at", label: "Timestamp", width: "170px", format: (v) => dateTime(v), title: (a) => a.created_at },
      { key: "actor", label: "Actor", width: "minmax(120px, 0.8fr)" },
      { key: "action", label: "Action", width: "minmax(180px, 1fr)", render: (a) => html`<span class="strong">${a.action}</span>` },
      { key: "details", label: "Details", width: "minmax(260px, 3fr)", title: (a) => JSON.stringify(a.details),
        render: (a) => html`<span class="mono xsmall">${JSON.stringify(a.details)}</span>` },
    ],
  });
  ctx.onCleanup(() => auditGrid.destroy());

  const drawAll = (d) => {
    if (!d) return;
    drawHeader(d);
    drawKpis(d);
    drawTelemetry(d);
    stmtGrid.setRows(d.statements || []);
    auditGrid.setRows(d.audit_logs || []);
  };
  drawHeader(data);
  drawKpis(data);
  drawTelemetry(data);

  // caseData is refreshed by store.mutated() anywhere in the app; redraw from it.
  ctx.onCleanup(ctx.store.select((s) => s.caseData, (d) => ctx.isCurrent() && drawAll(d)));
  ctx.onData([TOPICS.transactions, TOPICS.anomalies, TOPICS.suspense], () => bigGrid.refresh());

  ctx.registerCommands([
    { id: "overview.upload", title: "Upload statements", group: "Overview", icon: "upload", run: () => ctx.navigate("ingest") },
  ]);

  // everything above is released via ctx.onCleanup; nothing else to undo
  return () => {};
}
