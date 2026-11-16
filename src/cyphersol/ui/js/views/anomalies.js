/* Anomalies (#/case/{id}/anomalies?tab=<FLAG>&q=): the data-quality gate.
 *
 * - Loads every flagged row (GET transactions?flagged=true, ignored duplicates included so they can be restored).
 * - Flag-type cards (count + plain-language meaning) filter the grid; the choice is mirrored to ?tab=.
 * - Per-flag actions only where they make sense (UI-S-15):
 *     DUPLICATE_SUSPECT  -> Ignore / Restore (POST transactions/{tx}/toggle-duplicate) + Compare with twins
 *     BALANCE_MISMATCH   -> Balance context: neighbouring statement rows with expected vs printed balance
 *     DATE_*             -> Date context: neighbouring rows in statement order
 *   Row click opens the shared evidence drawer (provenance, narration, suggestions).
 * - Bulk de-duplication (FR-ING-06.2): POST dedup {apply:false} previews the SRS duplicate-key groups,
 *   {apply:true} ignores every non-kept row (audited BULK_DEDUP); idempotent.
 * - Rows are patched in place after a toggle (grid.updateRow); ctx.mutated() refreshes sidebar badges. */
import { isAbort } from "../core/api.js";
import { delegate, html, render, setVars } from "../core/dom.js";
import { DASH, date, inr, int, plural } from "../core/format.js";
import { MUTATION_TOPICS, TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { openDrawer } from "../components/drawer.js";
import { icon } from "../components/icons.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { openModal } from "../components/modal.js";
import { toast, withBusy } from "../components/toast.js";
import { FLAG_ORDER, flagInfo } from "./transactions-meta.js";
import {
  FLAG_HELP, accountCell, amountCell, flagChips, loadAllTransactions, narrationCell, openTxDrawer, statusCell, toggleDuplicate, txRowClass,
} from "./transactions.js";

export const styles = ["views/transactions.css"];

const ALL = "ALL";
const DATE_FLAGS = ["DATE_OUT_OF_ORDER", "DATE_UNPARSEABLE", "DATE_ORDER_AMBIGUOUS", "OUTSIDE_STATEMENT_PERIOD"];
const flagsOf = (t) => (t.anomaly_flags || []).filter((f) => f && f !== "VALID");

function shiftNotice(t) {
  const f = flagsOf(t);
  if (f.includes("BALANCE_MISMATCH")) return "balance";
  if (f.some((x) => DATE_FLAGS.includes(x))) return "date";
  return null;
}

export async function mount(el, ctx) {
  await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return;

  const state = { rows: [], total: 0, truncated: false };
  const stmtOf = (id) => ctx.store.statements().find((s) => s.statement_id === id) || null;
  const stmtName = (id) => {
    const s = stmtOf(id);
    return s ? `${s.account_holder_name || s.bank_name || "Account"} · ${s.bank_name || ""} ${s.account_number || ""}`.trim() : "Unknown statement";
  };

  render(
    el,
    html`<div class="stack-lg">
      <div data-slot="head"></div>
      <div data-slot="notice"></div>
      <div data-slot="body">${skeleton({ kind: "cards", rows: 3 })}${skeleton({ kind: "table", rows: 8 })}</div>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);
  render(slot("head"), pageHead({
    title: "Anomalies",
    sub: "Data-quality gate: running-balance, date-order, duplicate and OCR checks on every ingested row.",
    actions: html`<button type="button" class="btn btn--sm" data-action="dedup">${icon("swap", { size: "sm" })}Bulk de-duplicate…</button>
      <a class="btn btn--sm" href="${ctx.href("transactions", { flagged: "true" })}">${icon("list", { size: "sm" })}Open in Transactions</a>`,
  }));

  let grid = null;
  const destroyGrid = () => {
    grid?.destroy();
    grid = null;
  };
  ctx.onCleanup(destroyGrid);

  // ---------------------------------------------------------------- load
  let loadGen = 0;
  async function load() {
    const gen = ++loadGen;
    try {
      const res = await loadAllTransactions(ctx, { flagged: "true", sort: "txn_date", order: "asc" });
      if (!ctx.isCurrent() || gen !== loadGen) return;
      state.rows = res.rows;
      state.total = res.total;
      state.truncated = res.truncated;
      paint();
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || gen !== loadGen) return;
      destroyGrid();
      render(slot("body"), html`<div class="card"><div class="card__body">${errorState(err, { retryId: "reload", title: "Could not load flagged rows" })}</div></div>`);
    }
  }

  function counts() {
    const c = new Map();
    let ignored = 0;
    for (const t of state.rows) {
      for (const f of new Set(flagsOf(t))) c.set(f, (c.get(f) || 0) + 1);
      if (t.is_duplicate_ignored) ignored += 1;
    }
    return { c, ignored };
  }

  function paint() {
    render(slot("notice"), state.truncated
      ? html`<div class="callout callout--warn">${icon("alert")}<div>Showing the first ${int(state.rows.length)} of ${int(state.total)} flagged rows. Use Transactions → Flagged only for the full set.</div></div>`
      : "");
    if (!state.rows.length) {
      destroyGrid();
      render(slot("body"), html`<div class="card"><div class="card__body">${emptyState({
        icon: "check-circle", tone: "success", title: "No anomalies",
        message: "All statements passed the running-balance, date-order and duplicate checks with 0 anomalies.",
        action: { label: "Review transactions", href: ctx.href("transactions") },
      })}</div></div>`);
      return;
    }
    if (!grid) buildLayout();
    paintFlags();
    paintStatements();
    grid.setRows(state.rows);
    fitGrid();
  }

  /** Short lists should not leave a 600px empty frame. */
  function fitGrid() {
    if (!grid) return;
    setVars(grid.el, { "--dg-height": `min(64vh, ${Math.max(240, state.rows.length * 52 + 40)}px)` });
  }

  function buildLayout() {
    render(
      slot("body"),
      html`<div class="stack-lg">
        <section aria-labelledby="an-flags-title" class="stack-sm">
          <h2 class="sr-only" id="an-flags-title">Flag types</h2>
          <div class="v-anomalies-flags" role="group" aria-label="Filter by flag type" data-slot="flags"></div>
        </section>
        <section class="card" aria-labelledby="an-grid-title">
          <div class="card__head"><div><h2 class="card__title" id="an-grid-title">Flagged rows</h2>
            <p class="card__sub" data-slot="gridsub"></p></div></div>
          <div class="card__body" data-slot="grid"></div>
        </section>
        <section class="card" aria-labelledby="an-stmt-title">
          <div class="card__head"><div><h2 class="card__title" id="an-stmt-title">By statement</h2>
            <p class="card__sub">Where the flags come from. Click a statement to filter the rows above.</p></div></div>
          <div class="card__body card__body--flush" data-slot="stmts"></div>
        </section>
      </div>`
    );
    const statements = ctx.store.statements();
    const initialFlag = ctx.params.tab && ctx.params.tab !== ALL ? ctx.params.tab : "";
    grid = createDataGrid(slot("grid"), {
      rowClassName: txRowClass,
      ariaLabel: "Flagged transactions",
      rowKey: "tx_id",
      rows: state.rows,
      height: "min(64vh, 620px)",
      rowHeight: 52,
      countNoun: ["flagged row", "flagged rows"],
      initialSort: { key: "txn_date", order: "asc" },
      initialFilters: { flag: initialFlag, q: ctx.params.q || "" },
      filters: [
        { key: "q", type: "search", label: "Search flagged rows", placeholder: "Narration, party, UTR, holder…" },
        ...(statements.length > 1
          ? [{ key: "statement_id", type: "select", label: "Statement", options: [{ value: "", label: "All statements" }, ...statements.map((s) => ({ value: s.statement_id, label: stmtName(s.statement_id) }))] }]
          : []),
        { key: "hide_ignored", type: "toggle", label: "Hide ignored" },
      ],
      clientFilter: (t, f) => {
        if (f.flag && !flagsOf(t).includes(f.flag)) return false;
        if (f.statement_id && t.statement_id !== f.statement_id) return false;
        if (f.hide_ignored && t.is_duplicate_ignored) return false;
        const q = String(f.q || "").trim().toLowerCase();
        if (q && ![t.raw_narration, t.extracted_counterparty, t.utr_number, t.source_holder_name, t.cheque_or_ref_no].some((v) => v && String(v).toLowerCase().includes(q))) return false;
        return true;
      },
      onStateChange: ({ filters: f }) => {
        ctx.setParams({ tab: f.flag || null, q: f.q || null });
        paintFlags();
      },
      onRowClick: (t) => openDetail(t),
      emptyState: { icon: "check-circle", title: "No rows for this flag" },
      columns: [
        { key: "status", label: "QC", width: "44px", align: "center", render: statusCell },
        { key: "txn_date", label: "Date", width: "104px", sortable: true, format: (v) => date(v) },
        { key: "source_holder_name", label: "Account", width: "minmax(110px, 1fr)", sortable: true, render: accountCell },
        { key: "raw_narration", label: "Narration · party", width: "minmax(180px, 2.6fr)", sortable: true, render: narrationCell },
        {
          key: "amount", label: "Amount", width: "136px", align: "right", sortable: true, defaultOrder: "desc",
          sortValue: (t) => Math.max(Number(t.credit) || 0, Number(t.debit) || 0), render: amountCell,
          title: (t) => (t.credit > 0 ? `Credit ${inr(t.credit)}` : `Debit ${inr(t.debit)}`),
        },
        { key: "running_balance", label: "Balance", width: "132px", align: "right", sortable: true, format: (v) => inr(v) },
        {
          key: "anomaly_flags", label: "Flags", width: "minmax(190px, 1.2fr)",
          render: (t) => html`${flagChips(t, { max: 2 })}${t.is_duplicate_ignored ? html` <span class="badge badge--neutral">Ignored</span>` : ""}`,
        },
        { key: "actions", label: "Actions", width: "200px", align: "right", render: actionsCell },
      ],
    });

    delegate(grid.el, "click", "[data-action]", (e, b) => {
      const t = grid.rowFromEvent(e);
      if (!t) return;
      const a = b.dataset.action;
      if (a === "toggle-dup") withBusy(b, () => doToggle(t));
      else if (a === "compare") openCompare(t);
      else if (a === "context") openContext(t);
    });
  }

  function actionsCell(t) {
    const f = flagsOf(t);
    const dup = f.includes("DUPLICATE_SUSPECT") || t.is_duplicate_ignored;
    const ctxKind = shiftNotice(t);
    const compact = dup && ctxKind; // three actions: secondary ones become icon buttons
    const btns = [];
    if (dup) {
      btns.push(html`<button type="button" class="btn btn--sm ${t.is_duplicate_ignored ? "" : "btn--subtle"}" data-action="toggle-dup"
        aria-label="${t.is_duplicate_ignored ? "Restore" : "Ignore"} duplicate row of ${date(t.txn_date)}">${t.is_duplicate_ignored ? "Restore" : "Ignore dup"}</button>`);
      btns.push(compact
        ? html`<button type="button" class="btn btn--sm btn--ghost btn--icon" data-action="compare" aria-label="Compare with possible twins" title="Compare with possible twins">${icon("swap", { size: "sm" })}</button>`
        : html`<button type="button" class="btn btn--sm btn--ghost" data-action="compare">${icon("swap", { size: "sm" })}Compare</button>`);
    }
    if (ctxKind) {
      const label = ctxKind === "balance" ? "Balance context: neighbouring rows with expected vs printed balance" : "Date context: neighbouring rows in statement order";
      btns.push(compact
        ? html`<button type="button" class="btn btn--sm btn--ghost btn--icon" data-action="context" aria-label="${label}" title="${label}">${icon("table", { size: "sm" })}</button>`
        : html`<button type="button" class="btn btn--sm btn--ghost" data-action="context" title="${label}">${icon("table", { size: "sm" })}Context</button>`);
    }
    return btns.length ? html`<span class="v-anomalies-acts">${btns}</span>` : html`<span class="xsmall muted">Review details</span>`;
  }

  function paintFlags() {
    const host = slot("flags");
    if (!host) return;
    const { c, ignored } = counts();
    const active = grid ? grid.getFilters().flag || "" : "";
    const keys = FLAG_ORDER.filter((f) => c.has(f)).concat(Array.from(c.keys()).filter((f) => !FLAG_ORDER.includes(f)));
    render(host, html`
      <button type="button" class="v-anomalies-flag" data-flag="" aria-pressed="${active ? "false" : "true"}">
        <span class="row-between"><span class="badge badge--danger">All flags</span><span class="v-anomalies-flag__n num">${int(state.rows.length)}</span></span>
        <span class="xsmall text-2">${plural(state.rows.length, "flagged row")}${ignored ? ` · ${plural(ignored, "ignored duplicate")}` : ""}</span>
      </button>
      ${keys.map((f) => html`<button type="button" class="v-anomalies-flag" data-flag="${f}" aria-pressed="${active === f ? "true" : "false"}">
        <span class="row-between"><span class="badge badge--${flagInfo(f).tone}">${flagInfo(f).label}</span><span class="v-anomalies-flag__n num">${int(c.get(f))}</span></span>
        <span class="xsmall text-2 v-anomalies-flag__help">${FLAG_HELP[f] || ""}</span>
      </button>`)}`);
    const sub = slot("gridsub");
    if (sub) sub.textContent = active ? `${flagInfo(active).label}: ${FLAG_HELP[active] || ""}` : "Every row that failed at least one check. Actions appear only where they apply.";
  }

  function paintStatements() {
    const host = slot("stmts");
    if (!host) return;
    const by = new Map();
    for (const t of state.rows) {
      let r = by.get(t.statement_id);
      if (!r) {
        r = { id: t.statement_id, total: 0, balance: 0, dup: 0, date: 0, other: 0, ignored: 0 };
        by.set(t.statement_id, r);
      }
      const f = flagsOf(t);
      r.total += 1;
      if (f.includes("BALANCE_MISMATCH")) r.balance += 1;
      if (f.includes("DUPLICATE_SUSPECT")) r.dup += 1;
      if (f.some((x) => DATE_FLAGS.includes(x))) r.date += 1;
      if (f.some((x) => x !== "BALANCE_MISMATCH" && x !== "DUPLICATE_SUSPECT" && !DATE_FLAGS.includes(x))) r.other += 1;
      if (t.is_duplicate_ignored) r.ignored += 1;
    }
    const rows = Array.from(by.values()).sort((a, b) => b.total - a.total);
    render(host, html`<div class="table-wrap"><table class="table table--compact v-anomalies-stmts">
      <caption class="sr-only">Flagged rows per statement</caption>
      <thead><tr><th scope="col">Statement</th><th scope="col" class="num">Flagged</th><th scope="col" class="num">Balance</th><th scope="col" class="num">Duplicate</th>
        <th scope="col" class="num">Date</th><th scope="col" class="num">Other</th><th scope="col" class="num">Ignored</th><th scope="col"><span class="sr-only">Filter</span></th></tr></thead>
      <tbody>${rows.map((r) => {
        const s = stmtOf(r.id);
        return html`<tr>
          <td><span class="strong">${stmtName(r.id)}</span><span class="dg__sub">${s ? s.source_filename : r.id}</span></td>
          <td class="num strong">${int(r.total)}</td><td class="num">${r.balance ? int(r.balance) : DASH}</td><td class="num">${r.dup ? int(r.dup) : DASH}</td>
          <td class="num">${r.date ? int(r.date) : DASH}</td><td class="num">${r.other ? int(r.other) : DASH}</td><td class="num">${r.ignored ? int(r.ignored) : DASH}</td>
          <td class="num"><button type="button" class="btn btn--sm btn--ghost" data-stmt-filter="${r.id}">Show rows</button></td>
        </tr>`;
      })}</tbody></table></div>`);
  }

  // ---------------------------------------------------------------- actions
  let selfDepth = 0;
  async function selfMutated() {
    selfDepth += 1;
    try {
      await ctx.mutated(MUTATION_TOPICS.toggleDuplicate);
    } finally {
      selfDepth -= 1;
    }
  }

  async function doToggle(t, dw = null) {
    const res = await toggleDuplicate(ctx, t);
    if (!ctx.isCurrent()) return res;
    const patch = { is_duplicate_ignored: res.is_duplicate_ignored };
    const i = state.rows.findIndex((r) => r.tx_id === t.tx_id);
    if (i >= 0) state.rows[i] = { ...state.rows[i], ...patch };
    grid?.updateRow(t.tx_id, patch);
    dw?.update?.(patch);
    paintFlags();
    paintStatements();
    await selfMutated();
    return res;
  }

  let drawer = null;
  ctx.onCleanup(() => drawer && drawer.el.isConnected && drawer.close());

  function openDetail(t) {
    drawer = openTxDrawer(t, ctx, {
      actions: [
        { id: "toggle-dup", when: (x) => x.is_duplicate_ignored || flagsOf(x).includes("DUPLICATE_SUSPECT"), label: (x) => (x.is_duplicate_ignored ? "Restore row" : "Ignore duplicate"), icon: "eye", run: (x, dw) => doToggle(x, dw) },
        { id: "compare", when: (x) => flagsOf(x).includes("DUPLICATE_SUSPECT"), label: "Compare twins", icon: "swap", run: (x) => openCompare(x) },
        { id: "context", when: (x) => Boolean(shiftNotice(x)), label: (x) => (shiftNotice(x) === "balance" ? "Balance context" : "Date context"), icon: "table", run: (x) => openContext(x) },
      ],
    });
  }

  /** Neighbouring rows of the same statement (statement order) with expected vs printed running balance. */
  async function openContext(t) {
    const kind = shiftNotice(t) || "balance";
    const ctl = new AbortController();
    const d = openDrawer({
      title: kind === "balance" ? "Balance break context" : "Date sequence context",
      width: 820,
      className: "v-transactions-drawer",
      body: skeleton({ kind: "text", rows: 8 }),
      onClose: () => ctl.abort(),
    });
    drawer = d;
    try {
      // Fast path: a window in statement order (sort=row_index); row_index is the row's position in its
      // statement, so the target normally sits near offset row_index. Fall back to loading the statement.
      let rows = [];
      let truncated = false;
      let i = -1;
      if (Number.isFinite(t.row_index)) {
        const res = await ctx.api.get(
          "transactions",
          { statement_id: t.statement_id, sort: "row_index", order: "asc", offset: Math.max(0, t.row_index - 8), limit: 17 },
          { signal: ctl.signal }
        );
        rows = res.transactions || [];
        i = rows.findIndex((r) => r.tx_id === t.tx_id);
      }
      if (i < 0) {
        ({ rows, truncated } = await loadAllTransactions(ctx, { statement_id: t.statement_id, sort: "row_index", order: "asc" }, { signal: ctl.signal, cap: 40000 }));
        rows.sort((a, b) => (a.row_index ?? 0) - (b.row_index ?? 0));
        i = rows.findIndex((r) => r.tx_id === t.tx_id);
      }
      if (!d.el.isConnected) return;
      if (i < 0) {
        d.setBody(html`<div class="callout callout--warn">${icon("alert")}<div>This row could not be located in its statement${truncated ? " (statement too large to load in full)" : ""}.</div></div>`);
        return;
      }
      const lo = Math.max(0, i - 5);
      const hi = Math.min(rows.length - 1, i + 5);
      const win = [];
      for (let k = lo; k <= hi; k += 1) {
        const r = rows[k];
        const prev = k > 0 ? rows[k - 1] : null;
        const expected = prev ? (Number(prev.running_balance) || 0) + (Number(r.credit) || 0) - (Number(r.debit) || 0) : null;
        let delta = expected == null ? null : (Number(r.running_balance) || 0) - expected;
        if (delta != null && Math.abs(delta) < 0.005) delta = 0;
        const dateBack = prev && r.txn_date && prev.txn_date && r.txn_date < prev.txn_date;
        win.push({ r, expected, delta, dateBack, target: k === i });
      }
      const me = win.find((w) => w.target);
      const s = stmtOf(t.statement_id);
      d.setBody(html`<div class="stack">
        <div class="callout ${kind === "balance" ? "callout--danger" : "callout--warn"}">${icon("alert")}<div class="small">
          ${kind === "balance"
            ? me && me.delta != null
              ? html`Printed balance on row ${int((t.row_index ?? i) + 1)} differs from the expected balance by <strong class="num">${inr(me.delta, { sign: true })}</strong>.
                  Likely causes: rows missing between the previous row and this one, an OCR mis-read, or an altered statement.`
              : html`This is the first row of the statement, so the break is measured against the opening balance.`
            : html`${FLAG_HELP[flagsOf(t).find((f) => DATE_FLAGS.includes(f))] || "Date flagged."} Rows are shown in statement order.`}
        </div></div>
        <p class="xsmall muted">${s ? s.source_filename : ""} · rows ${int(lo + 1)}–${int(hi + 1)} of ${int(rows.length)} in statement order. Expected = previous balance + credit − debit.</p>
        <div class="table-wrap"><table class="table table--compact v-anomalies-ctx">
          <caption class="sr-only">Statement rows around the flagged row, with expected and printed balances</caption>
          <thead><tr><th scope="col" class="num">Row</th><th scope="col">Date</th><th scope="col">Narration</th><th scope="col" class="num">Debit</th><th scope="col" class="num">Credit</th>
            <th scope="col" class="num">Printed balance</th><th scope="col" class="num">Expected</th><th scope="col" class="num">Difference</th></tr></thead>
          <tbody>${win.map((w) => html`<tr class="${w.target ? "v-anomalies-target" : ""}" ${w.target ? html`aria-current="true"` : ""}>
            <td class="num">${int((w.r.row_index ?? 0) + 1)}${w.target ? html` <span class="sr-only">(flagged row)</span>${icon("arrow-left", { size: "sm" })}` : ""}</td>
            <td class="nowrap">${date(w.r.txn_date)}${w.dateBack ? html` <span class="badge badge--warn" title="Earlier than the previous row">${icon("alert", { size: "sm" })}earlier</span>` : ""}</td>
            <td class="v-anomalies-ctx__narr">${w.r.raw_narration}${flagsOf(w.r).length && !w.target ? html` ${flagChips(w.r, { max: 1 })}` : ""}</td>
            <td class="num ${w.r.debit > 0 ? "is-debit" : "muted"}">${w.r.debit > 0 ? inr(w.r.debit) : DASH}</td>
            <td class="num ${w.r.credit > 0 ? "is-credit" : "muted"}">${w.r.credit > 0 ? inr(w.r.credit) : DASH}</td>
            <td class="num">${inr(w.r.running_balance)}</td>
            <td class="num text-2">${w.expected == null ? DASH : inr(w.expected)}</td>
            <td class="num ${w.delta ? "strong " + (w.delta < 0 ? "is-neg" : "is-pos") : "muted"}">${w.delta == null ? DASH : w.delta === 0 ? "0" : inr(w.delta, { sign: true })}</td>
          </tr>`)}</tbody>
        </table></div>
      </div>`);
    } catch (err) {
      if (isAbort(err) || !d.el.isConnected) return;
      d.setBody(errorState(err, { retryId: "", title: "Could not load statement rows", compact: true }));
    }
  }

  /** Rows with the same date, amount and direction anywhere in the case, side by side with this one. */
  async function openCompare(t) {
    const ctl = new AbortController();
    const d = openDrawer({ title: "Duplicate comparison", width: 860, className: "v-transactions-drawer", body: skeleton({ kind: "text", rows: 8 }), onClose: () => ctl.abort() });
    drawer = d;
    const amount = Math.max(Number(t.debit) || 0, Number(t.credit) || 0);
    const paintCompare = (twins) => {
      const cols = [t, ...twins.slice(0, 3)];
      const amt = (r) => (r.credit > 0 ? `+${inr(r.credit)}` : inr(-(r.debit || 0)));
      const fields = [
        ["Statement", (r) => (stmtOf(r.statement_id) || {}).source_filename || r.statement_id],
        ["Account", (r) => `${r.source_holder_name || DASH} · ${r.source_account_number || DASH}`],
        ["Row in statement", (r) => (Number.isFinite(r.row_index) ? int(r.row_index + 1) : DASH)],
        ["Date", (r) => date(r.txn_date)],
        ["Narration", (r) => r.raw_narration],
        ["Amount", amt],
        ["Balance after", (r) => inr(r.running_balance)],
        ["UTR / ref", (r) => r.utr_number || r.cheque_or_ref_no || DASH],
        ["Channel", (r) => r.payment_channel || DASH],
        ["Status", (r) => (r.is_duplicate_ignored ? "Ignored" : flagsOf(r).includes("DUPLICATE_SUSPECT") ? "Duplicate suspect" : "Counted")],
      ];
      d.setBody(html`<div class="stack">
        ${twins.length
          ? html`<p class="small text-2">${plural(twins.length, "other row")} share this date, amount and direction. Matching values are marked <span class="v-anomalies-same-key">=</span>.
              Keep one copy counted and ignore the others.</p>`
          : html`<div class="callout callout--info">${icon("info")}<div class="small">No other row in this case has the same date, amount and direction. The duplicate check may have matched on narration within an overlapping statement period — review before ignoring.</div></div>`}
        <div class="table-wrap"><table class="table table--compact v-anomalies-cmp">
          <caption class="sr-only">Field-by-field comparison of the flagged row and possible duplicates</caption>
          <thead><tr><th scope="col">Field</th>${cols.map((r, i) => html`<th scope="col">${i === 0 ? "This row" : `Twin ${i}`}</th>`)}</tr></thead>
          <tbody>${fields.map(([label, fn]) => html`<tr><th scope="row" class="small text-2">${label}</th>${cols.map((r, i) => {
            const v = String(fn(r) ?? DASH);
            const same = i > 0 && v === String(fn(t) ?? DASH);
            return html`<td class="${same ? "v-anomalies-same" : ""} ${label === "Amount" || label === "Balance after" ? "num" : ""}">${same ? html`<span class="v-anomalies-same-key" aria-hidden="true">=</span><span class="sr-only">(same as this row)</span> ` : ""}${v}</td>`;
          })}</tr>`)}
          <tr><th scope="row" class="small text-2">Action</th>${cols.map((r) => html`<td>${r.is_duplicate_ignored || flagsOf(r).includes("DUPLICATE_SUSPECT")
            ? html`<button type="button" class="btn btn--sm ${r.is_duplicate_ignored ? "" : "btn--subtle"}" data-cmp-toggle="${r.tx_id}">${r.is_duplicate_ignored ? "Restore" : "Ignore"}</button>`
            : html`<span class="xsmall muted">Counted</span>`}</td>`)}</tr>
          </tbody></table></div>
      </div>`);
    };
    let twins = [];
    try {
      const res = await ctx.api.get("transactions", { date_from: t.txn_date, date_to: t.txn_date, amount_min: amount, amount_max: amount, direction: t.direction, limit: 50 }, { signal: ctl.signal });
      if (!d.el.isConnected) return;
      // same statement first (overlap inside one file), then same account, then the rest of the case
      const rank = (r) => (r.statement_id === t.statement_id ? 0 : r.source_account_number === t.source_account_number ? 1 : 2);
      twins = (res.transactions || []).filter((r) => r.tx_id !== t.tx_id).sort((a, b) => rank(a) - rank(b));
      paintCompare(twins);
    } catch (err) {
      if (isAbort(err) || !d.el.isConnected) return;
      d.setBody(errorState(err, { retryId: "", title: "Could not load matching rows", compact: true }));
      return;
    }
    d.el.addEventListener("click", (e) => {
      const b = e.target.closest("[data-cmp-toggle]");
      if (!b) return;
      const id = b.dataset.cmpToggle;
      const row = id === t.tx_id ? t : twins.find((r) => r.tx_id === id);
      if (!row) return;
      withBusy(b, async () => {
        const res = await doToggle(row);
        if (!res) return;
        if (row === t) t = { ...t, is_duplicate_ignored: res.is_duplicate_ignored };
        else twins = twins.map((r) => (r.tx_id === id ? { ...r, is_duplicate_ignored: res.is_duplicate_ignored } : r));
        if (d.el.isConnected) paintCompare(twins);
      });
    });
  }

  // ---------------------------------------------------------------- bulk de-duplication (FR-ING-06.2)
  const dedupGroupRows = (groups) => groups.slice(0, 200).map((g) => html`<tr>
      <td>${date(g.txn_date)}</td>
      <td class="mono">${g.account_number || DASH}</td>
      <td><span class="badge ${g.direction === "CR" ? "badge--success" : "badge--neutral"}">${g.direction === "CR" ? "Cr" : "Dr"}</span></td>
      <td class="num">${inr(g.amount)}</td>
      <td class="num">${int((g.duplicate_tx_ids || []).length)}</td>
      <td class="num">${int((g.statement_ids || []).length)}</td>
    </tr>`);

  async function openDedup(btn) {
    const preview = await withBusy(btn, () => ctx.api.post("dedup", { apply: false }), { error: "Could not preview duplicates" });
    if (!preview || !ctx.isCurrent()) return;
    const groups = preview.groups || [];
    const n = Number(preview.duplicate_count) || 0;
    const m = openModal({
      title: "Bulk de-duplicate",
      description: "Rows with the same account, date, amount, direction and narration (the SRS duplicate key) across overlapping statements.",
      size: "lg",
      body: groups.length
        ? html`<div class="stack">
            <p>${plural(Number(preview.group_count) || groups.length, "duplicate group")} · <strong>${plural(n, "row")}</strong> would be ignored. The first occurrence of each group is kept; ignored rows are excluded from totals, Tally and analytics and can be restored one by one.</p>
            <div class="table-wrap table-wrap--max"><table class="table table--compact">
              <caption class="sr-only">Duplicate groups</caption>
              <thead><tr><th scope="col">Date</th><th scope="col">Account</th><th scope="col">Dr/Cr</th><th scope="col" class="num">Amount</th><th scope="col" class="num">Duplicates</th><th scope="col" class="num">Statements</th></tr></thead>
              <tbody>${dedupGroupRows(groups)}</tbody>
            </table></div>
            ${groups.length > 200 ? html`<p class="small muted">Showing the first 200 of ${int(groups.length)} groups.</p>` : ""}
          </div>`
        : html`<div class="callout callout--success">${icon("check-circle")}<div>No unhandled duplicates: every duplicate-key group is already reduced to one row.</div></div>`,
      actions: groups.length && n
        ? [{ label: "Cancel", value: "cancel" }, { label: `Ignore ${plural(n, "duplicate")}`, variant: "primary", type: "submit" }]
        : [{ label: "Close", value: "close" }],
      onSubmit: async (_v, { el: dlg }) => {
        const submit = dlg.querySelector('[type="submit"]');
        const res = await withBusy(submit, () => ctx.api.post("dedup", { apply: true }), { error: "De-duplication failed" });
        if (!res) return false;
        return res;
      },
    });
    const res = await m.closed;
    if (!res || typeof res !== "object" || !ctx.isCurrent()) return;
    const changed = (res.changed_tx_ids || []).length;
    toast.success(changed ? `${plural(changed, "row")} ignored as duplicates (audited as BULK_DEDUP).` : "Nothing changed: the duplicates were already ignored.", { title: "Bulk de-duplication" });
    await ctx.mutated(MUTATION_TOPICS.toggleDuplicate);
  }

  // ---------------------------------------------------------------- page events
  ctx.onCleanup(delegate(slot("head"), "click", '[data-action="dedup"]', (_e, b) => openDedup(b)));
  ctx.onCleanup(
    delegate(el, "click", "[data-flag], [data-stmt-filter], [data-action='reload']", (_e, b) => {
      if (b.dataset.action === "reload") {
        destroyGrid();
        render(slot("body"), skeleton({ kind: "table", rows: 8 }));
        load();
        return;
      }
      if (!grid) return;
      if (b.hasAttribute("data-stmt-filter")) {
        grid.setFilters({ statement_id: b.dataset.stmtFilter });
        slot("grid")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        return;
      }
      grid.setFilters({ flag: b.dataset.flag || "" });
      paintFlags();
    })
  );

  ctx.onData([TOPICS.anomalies, TOPICS.transactions, TOPICS.statements], () => {
    if (selfDepth > 0 || !ctx.isCurrent()) return;
    load();
  });

  ctx.registerCommands([
    { id: "anomalies.all", title: "Show all flagged rows", group: "Anomalies", icon: "alert", run: () => grid?.setFilters({ flag: "" }) },
    { id: "anomalies.balance", title: "Show balance mismatches", group: "Anomalies", icon: "alert", run: () => grid?.setFilters({ flag: "BALANCE_MISMATCH" }) },
    { id: "anomalies.duplicates", title: "Show duplicate suspects", group: "Anomalies", icon: "alert", run: () => grid?.setFilters({ flag: "DUPLICATE_SUSPECT" }) },
    { id: "anomalies.dedup", title: "Bulk de-duplicate…", group: "Anomalies", icon: "swap", run: () => openDedup(null) },
    { id: "anomalies.hide-ignored", title: "Hide ignored duplicates", group: "Anomalies", icon: "eye", run: () => grid?.setFilters({ hide_ignored: "true" }) },
  ]);

  await load();
  if (!ctx.isCurrent()) return;

  return {
    update(params) {
      if (!grid) return;
      const want = { flag: params.tab && params.tab !== ALL ? params.tab : "", q: params.q || "" };
      const cur = grid.getFilters();
      if ((cur.flag || "") !== want.flag || (cur.q || "") !== want.q) {
        grid.setFilters(want, { silent: true });
        paintFlags();
      }
    },
  };
}
