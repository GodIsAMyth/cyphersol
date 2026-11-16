/* CypherEdge · ITR (#/case/{id}/itr?fy=FY%202025-26)
 *
 * FY-scoped ITR summary (GET edge/itr?fy=). P1 (EDGE-15..20): every statutory check comes from the server's
 * checks_run[] {id, group, section, title, rule, thresholds, status pass|verify|flag|no_data, flag_count, severity,
 * amount, caveats} — the UI holds no thresholds of its own; red flags join their check through check_id. Also shown:
 * deductions[] (section, cap, eligible amount, transactions) with deduction_note, the SFT checks (cash meters use the
 * SFT-003 thresholds from the payload), the reconciliation_waterfall from gross credits to unexplained credits
 * (reconciliation.reconciles / unexplained_credits), income heads and the 52-category schedule.
 * Export: export/itr-xlsx?fy= (Blob, audit-logged).
 */
import { isAbort } from "../core/api.js";
import { delegate, html, render, setVars } from "../core/dom.js";
import { bytes, catNo, date, DASH, inr, inrCompact, int, pct, plural } from "../core/format.js";
import { TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";

export const styles = ["views/edge.css"];

const fyStart = (label) => {
  const m = /(\d{4})/.exec(label || "");
  return m ? Number(m[1]) : null;
};
const fyRange = (label) => {
  const y = fyStart(label);
  return y ? { date_from: `${y}-04-01`, date_to: `${y + 1}-03-31` } : {};
};

const STATUS = {
  pass: { label: "Pass", tone: "success", icon: "check-circle" },
  verify: { label: "Verify", tone: "warn", icon: "alert" },
  flag: { label: "Flag", tone: "danger", icon: "x-circle" },
  no_data: { label: "No data", tone: "neutral", icon: "minus" },
};
const statusMeta = (s) => STATUS[s] || { label: String(s || "—"), tone: "neutral", icon: "info" };
const GROUP_ORDER = ["TDS", "Cash", "SFT", "Other"];
const sevTone = (s) => ({ CRITICAL: "critical", HIGH: "danger", MEDIUM: "warn", LOW: "info" })[String(s || "").toUpperCase()] || "neutral";

/** Human text for one thresholds{} object from checks_run (values come from the server). */
function thresholdChips(t) {
  if (!t || typeof t !== "object") return [];
  const ge = t.inclusive ? "≥" : ">";
  const per = t.period === "month" ? "per month" : t.period === "fy" ? "FY aggregate" : "";
  const out = [];
  const money = (v) => inr(v, { decimals: 0 });
  if (t.single_limit != null) out.push(`single payment > ${money(t.single_limit)}`);
  if (t.aggregate_limit != null) out.push(`${per || "aggregate"} > ${money(t.aggregate_limit)}`);
  if (t.per_party_per_day != null) out.push(`per party per day ${ge} ${money(t.per_party_per_day)}`);
  if (t.transporter_per_day != null) out.push(`transporters ${ge} ${money(t.transporter_per_day)}`);
  if (t.per_person_per_day != null) out.push(`per person per day ${ge} ${money(t.per_person_per_day)}`);
  if (t.non_current_deposits != null) out.push(`non-current deposits ${ge} ${money(t.non_current_deposits)}`);
  if (t.current_deposits != null) out.push(`current deposits ${ge} ${money(t.current_deposits)}`);
  if (t.current_withdrawals != null) out.push(`current withdrawals ${ge} ${money(t.current_withdrawals)}`);
  if (t.fy_aggregate != null) out.push(`FY aggregate ${ge} ${money(t.fy_aggregate)}`);
  if (t.per_party_fy_aggregate != null) out.push(`per party FY ${ge} ${money(t.per_party_fy_aggregate)}`);
  if (t.any_amount != null) out.push("any amount");
  if (t.rate_note) out.push(`rate ${t.rate_note}`);
  if (t.applies_from_fy_start || t.applies_to_fy_start) {
    const from = t.applies_from_fy_start && t.applies_from_fy_start > 2000 ? `from FY ${t.applies_from_fy_start}-${String((t.applies_from_fy_start + 1) % 100).padStart(2, "0")}` : "";
    const to = t.applies_to_fy_start ? `until FY ${t.applies_to_fy_start}-${String((t.applies_to_fy_start + 1) % 100).padStart(2, "0")}` : "";
    if (from || to) out.push([from, to].filter(Boolean).join(" "));
  }
  return out;
}

export async function mount(el, ctx) {
  let fy = ctx.params.fy || "";
  let data = null;
  let ctl = null;
  let gen = 0;
  let schedGrid = null;
  ctx.onCleanup(() => {
    ctl?.abort();
    schedGrid?.destroy();
  });

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "ITR compliance",
        sub: "Income heads, deductions, SFT reporting and statutory checks for one financial year, derived from categorised bank transactions.",
        actions: html`<div class="field field--inline v-itr-fy">
          <label class="field__label" for="itr-fy">Financial year</label>
          <select class="select" id="itr-fy" disabled><option>Loading…</option></select>
        </div>
        <button type="button" class="btn" data-action="itr-xlsx" disabled>${icon("table", { size: "sm" })}Excel</button>`,
      })}
      <div data-slot="body">${skeleton({ kind: "kpis", rows: 6 })}${skeleton({ kind: "table", rows: 9 })}</div>
    </div>`
  );
  const body = el.querySelector('[data-slot="body"]');
  const fySel = el.querySelector("#itr-fy");
  const xlsxBtn = el.querySelector('[data-action="itr-xlsx"]');

  async function load() {
    ctl?.abort();
    ctl = new AbortController();
    const myGen = ++gen;
    body.setAttribute("aria-busy", "true");
    if (!data) render(body, html`${skeleton({ kind: "kpis", rows: 6 })}${skeleton({ kind: "table", rows: 9 })}`);
    try {
      const res = await ctx.api.get("edge/itr", { fy: fy || null }, { signal: ctl.signal });
      if (!ctx.isCurrent() || myGen !== gen) return;
      data = res;
      draw();
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || myGen !== gen) return;
      data = null;
      schedGrid?.destroy();
      schedGrid = null;
      xlsxBtn.disabled = true;
      const badFy = err?.isValidation && fy;
      render(
        body,
        html`<div class="card"><div class="card__body">${errorState(err, { retryId: badFy ? "itr-latest" : "itr-retry", title: badFy ? `No report for ${fy}` : "Could not build the ITR report" })}</div></div>`
      );
      if (badFy) {
        const b = body.querySelector('[data-action="itr-latest"]');
        if (b) b.lastChild.textContent = "Show latest financial year";
      }
      fySel.disabled = !badFy;
    } finally {
      if (myGen === gen) body.removeAttribute("aria-busy");
    }
  }

  function drawFySelect() {
    const fys = data.available_fys || [];
    const cur = data.financial_year;
    const list = fys.includes(cur) || !cur ? fys : [cur, ...fys];
    render(fySel, list.length ? html`${list.map((f) => html`<option value="${f}" ${f === cur ? html`selected` : ""}>${f}</option>`)}` : html`<option value="">No dated transactions</option>`);
    fySel.disabled = list.length < 2;
  }

  /** checks_run + their red flags (joined by check_id); flags without a known check still surface. */
  function checkRows(d) {
    const flags = d.statutory_red_flags || [];
    const checks = d.checks_run || [];
    const ids = new Set(checks.map((c) => c.id));
    const rows = checks.map((c) => ({ ...c, flags: flags.filter((f) => f.check_id === c.id) }));
    flags.filter((f) => !ids.has(f.check_id)).forEach((f, i) => rows.push({
      id: f.check_id || `other-${i}`, group: "Other", section: f.section, title: f.title, rule: "", thresholds: null,
      status: f.verify_source ? "verify" : "flag", flag_count: 1, severity: f.severity, amount: f.amount, caveats: [], flags: [f],
    }));
    return rows;
  }

  function draw() {
    drawFySelect();
    const d = data;
    const hasData = (d.transaction_count || 0) > 0;
    xlsxBtn.disabled = !hasData;
    if (!hasData && !(d.available_fys || []).length) {
      schedGrid?.destroy();
      schedGrid = null;
      render(body, html`<div class="card"><div class="card__body">${emptyState({
        icon: "receipt", title: "No transactions to assess",
        message: "Upload bank statements for this case; the ITR summary is computed per Indian financial year (April–March).",
        action: { label: "Upload statements", href: ctx.href("ingest") },
      })}</div></div>`);
      return;
    }
    const r = d.reconciliation || {};
    const inc = d.income_heads || {};
    const rows = checkRows(d);
    const count = (k) => rows.filter((x) => x.status === k).length;
    const nFlag = count("flag");
    const nVerify = count("verify");
    const nPass = count("pass");
    const range = fyRange(d.financial_year);
    const groups = [...GROUP_ORDER, ...new Set(rows.map((x) => x.group).filter((g) => !GROUP_ORDER.includes(g)))]
      .map((g) => ({ g, items: rows.filter((x) => (x.group || "Other") === g) }))
      .filter((x) => x.items.length);
    const deds = d.deductions || [];

    render(
      body,
      html`<div class="stack-lg">
        ${kpiGrid(
          [
            { label: "Transactions in FY", value: int(d.transaction_count), icon: "list", href: ctx.href("transactions", range),
              sub: d.excluded_other_fy_count ? `${int(d.excluded_other_fy_count)} in other FYs excluded` : d.financial_year },
            { label: "Gross credits", value: inrCompact(r.gross_credits), title: inr(r.gross_credits), icon: "arrow-down-left" },
            { label: "Gross debits", value: inrCompact(r.gross_debits), title: inr(r.gross_debits), icon: "arrow-up-right" },
            { label: "Net business receipts", value: inrCompact(r.net_business_and_professional_receipts), title: inr(r.net_business_and_professional_receipts), tone: "accent" },
            { label: "Unexplained credits", value: inrCompact(r.unexplained_credits), title: inr(r.unexplained_credits),
              tone: r.unexplained_credits ? "warn" : "success", sub: "uncategorised credits to explain" },
            { label: "Statutory checks", value: nFlag + nVerify ? `${int(nFlag)} flag · ${int(nVerify)} verify` : `${int(nPass)} pass`,
              tone: nFlag ? "danger" : nVerify ? "warn" : "success", icon: "shield", sub: `${plural(rows.length, "check")} run` },
            { label: "Suspense pending", value: int(d.suspense_pending_count), tone: d.suspense_pending_count ? "warn" : "success",
              sub: d.suspense_pending_count ? "may hide income or TDS items" : "all rows categorised", href: ctx.href("suspense") },
          ],
          { label: `ITR summary for ${d.financial_year}` }
        )}
        ${d.suspense_pending_count
          ? html`<div class="callout callout--warn">${icon("alert")}<div><strong>${plural(d.suspense_pending_count, "transaction")} still in suspense.</strong>
              Uncategorised rows are not counted in income heads or TDS checks; a “Pass” marked provisional below may change once they are resolved in <a href="${ctx.href("suspense")}">Suspense</a>.</div></div>`
          : ""}

        <section class="card" aria-labelledby="itr-checks-h">
          <div class="card__head"><div><h2 class="card__title" id="itr-checks-h">Statutory compliance checks · ${d.financial_year}</h2>
            <p class="card__sub">Every check the engine ran for this FY, with the thresholds it applied. Candidates for verification, not a determination of liability.</p></div>
            <div class="legend" aria-label="Status counts">
              <span class="badge badge--danger">${int(nFlag)} Flag</span><span class="badge badge--warn">${int(nVerify)} Verify</span><span class="badge badge--success">${int(nPass)} Pass</span>
            </div></div>
          <div class="card__body card__body--flush">${groups.map(({ g, items }) => html`<h3 class="subhead v-itr-group">${g === "SFT" ? "SFT · Statement of Financial Transactions (Rule 114E)" : g === "TDS" ? "TDS" : g === "Cash" ? "Cash limits" : g}</h3>
            <ul class="v-itr-checks" role="list">${items.map((c) => checkRow(c, d))}</ul>`)}</div>
        </section>

        <section class="card" aria-labelledby="itr-recon-h">
          <div class="card__head"><div><h2 class="card__title" id="itr-recon-h">Receipts reconciliation</h2>
            <p class="card__sub">Gross bank credits stepped down to the income heads; what is left is unexplained.</p></div>
            ${r.reconciles === false
              ? html`<span class="badge badge--danger">Does not reconcile</span>`
              : html`<span class="badge badge--success">${icon("check", { size: "sm" })}Reconciles</span>`}</div>
          <div class="card__body card__body--flush">${waterfall(d)}</div>
        </section>

        <div class="grid-2 v-itr-cols">
          <section class="card" aria-labelledby="itr-income-h">
            <div class="card__head"><h2 class="card__title" id="itr-income-h">Income heads</h2></div>
            <div class="card__body card__body--flush"><div class="table-wrap"><table class="table table--compact">
              <caption class="sr-only">Income heads for ${d.financial_year}</caption>
              <thead><tr><th scope="col">Head</th><th scope="col">ITR schedule</th><th scope="col" class="num">Amount</th></tr></thead>
              <tbody>
                ${moneyRow("Salary", "Sec 15–17", inc.salary_sec_15)}
                ${moneyRow("Business / profession", "PGBP · 44AD / 44ADA", inc.business_profession_pgbp)}
                ${moneyRow("House property (rent received)", "Sec 22", inc.house_property_rent)}
                ${moneyRow("Interest income", "Sec 80TTA / 80TTB · OS", inc.interest_income_80tta_80ttb)}
                ${moneyRow("Dividend income", "Schedule OS", inc.dividend_income)}
              </tbody></table></div></div>
          </section>
          <section class="card" aria-labelledby="itr-ded-h">
            <div class="card__head"><div><h2 class="card__title" id="itr-ded-h">Deductions</h2>
              <p class="card__sub">Bank-side candidates with the statutory cap and the amount eligible up to it.</p></div></div>
            <div class="card__body stack-sm">
              ${d.deduction_note ? html`<div class="callout callout--info">${icon("info")}<div class="small">${d.deduction_note}</div></div>` : ""}
              ${deds.length ? deductionTable(deds) : emptyState({ icon: "receipt", title: "No deduction candidates", message: "No insurance, ELSS, PF, home-loan or similar outflows in this FY.", compact: true })}
            </div>
          </section>
        </div>

        <section class="card" aria-labelledby="itr-sched-h">
          <div class="card__head"><div><h2 class="card__title" id="itr-sched-h">Category schedule</h2>
            <p class="card__sub">CypherSOL's 52-category taxonomy mapped to ITR sections. Select a row to open its transactions for this FY.</p></div></div>
          <div class="card__body" data-slot="sched"></div>
        </section>
      </div>`
    );
    drawSchedule(d, range);
  }

  function checkRow(c, d) {
    const st = statusMeta(c.status);
    const chips = thresholdChips(c.thresholds);
    const caveats = c.caveats || [];
    const sft3 = c.thresholds && c.thresholds.non_current_deposits != null;
    return html`<li class="v-itr-check" data-status="${c.status}" data-check="${c.id}">
      <div class="v-itr-check__status"><span class="badge badge--${st.tone} badge--lg">${icon(st.icon, { size: "sm" })}${st.label}</span></div>
      <div class="v-itr-check__main min-w-0">
        <div class="v-itr-check__title"><span class="mono xsmall text-2">${c.section}</span> <span class="strong">${c.title}</span>
          ${c.severity && !c.flags.length ? html` <span class="badge badge--${sevTone(c.severity)}">${String(c.severity).toUpperCase()}</span>` : ""}</div>
        ${c.rule ? html`<div class="xsmall muted">${c.rule}</div>` : ""}
        ${chips.length ? html`<div class="v-itr-thresholds" aria-label="Thresholds applied">${chips.map((t) => html`<span class="badge badge--outline">${t}</span>`)}</div>` : ""}
        ${caveats.length ? html`<ul class="v-itr-caveats xsmall">${caveats.map((t) => html`<li>${t}</li>`)}</ul>` : ""}
        ${sft3 ? sftMeters(d.cash_summary || {}, c.thresholds) : ""}
        ${c.flags.map((f) => flagDetail(f))}
      </div>
      <div class="v-itr-check__amt num" ${c.amount ? html`title="${inr(c.amount)}"` : ""}>${c.amount ? inr(c.amount) : html`<span class="muted">${DASH}</span>`}</div>
    </li>`;
  }

  /** SFT-003 cash meters; limits are the check's own thresholds from checks_run. */
  function sftMeters(cash, t) {
    const items = [
      { label: "Cash deposits · non-current accounts", v: cash.cash_deposits_non_current || 0, limit: Number(t.non_current_deposits) || 0 },
      { label: "Cash deposits · current accounts", v: cash.cash_deposits_current || 0, limit: Number(t.current_deposits) || 0 },
      { label: "Cash withdrawals · current accounts", v: cash.cash_withdrawals_current || 0, limit: Number(t.current_withdrawals) || 0 },
    ].filter((m) => m.limit > 0);
    return html`<div class="v-itr-meters">
      ${items.map((m) => {
        const ratio = m.v / m.limit;
        return html`<div class="v-itr-meter">
          <div class="row-between xsmall"><span class="text-2">${m.label}</span>
            <span class="tabular"><span title="${inr(m.v)}">${inrCompact(m.v)}</span> <span class="muted">of ${inrCompact(m.limit)} · ${pct(ratio, { decimals: 0 })}</span></span></div>
          <div class="meter ${ratio >= 1 ? "meter--critical" : ratio >= 0.8 ? "meter--suspicious" : "meter--low"}" role="meter"
            aria-label="${m.label}" aria-valuemin="0" aria-valuemax="${m.limit}" aria-valuenow="${Math.round(m.v)}"
            aria-valuetext="${inr(m.v)} of ${inr(m.limit)}" data-value="${Math.min(100, ratio * 100).toFixed(1)}"><div class="meter__fill"></div></div>
        </div>`;
      })}
      ${cash.account_types_known === false ? html`<div class="xsmall muted">Account type unknown: all accounts treated as non-current.</div>` : ""}
    </div>`;
  }

  /** Waterfall as a table: every step has its amount, the running balance and a bar (the table is the text). */
  function waterfall(d) {
    const steps = d.reconciliation_waterfall || [];
    if (!steps.length) return html`<div class="card__body">${emptyState({ icon: "chart", title: "No reconciliation steps", compact: true })}</div>`;
    const top = Math.max(1, ...steps.map((s) => Math.max(Number(s.amount) || 0, Number(s.running_balance) || 0)));
    const range = fyRange(d.financial_year);
    return html`<div class="table-wrap"><table class="table table--compact v-itr-wf">
      <caption class="sr-only">Reconciliation waterfall from gross credits to unexplained credits for ${d.financial_year}</caption>
      <thead><tr><th scope="col">Step</th><th scope="col" class="num">Amount</th><th scope="col" class="v-itr-wf__barcol"><span class="sr-only">Bar</span></th><th scope="col" class="num">Remaining</th></tr></thead>
      <tbody>${steps.map((s) => {
        const amt = Number(s.amount) || 0;
        const run = Number(s.running_balance) || 0;
        const isLevel = s.kind === "start" || s.kind === "subtotal";
        const x0 = isLevel ? 0 : s.kind === "unexplained" ? 0 : run;
        const w = isLevel ? run : amt;
        const cats = s.categories || [];
        const link = cats.length && amt ? ctx.href("transactions", { category_id: cats.length === 1 ? cats[0] : null, direction: "CR", ...range }) : "";
        return html`<tr class="v-itr-wf__row v-itr-wf__row--${s.kind} ${amt ? "" : "is-zero"}" data-x0="${((x0 / top) * 100).toFixed(2)}" data-w="${((w / top) * 100).toFixed(2)}">
          <th scope="row" class="small">${s.label}${cats.length ? html`<span class="dg__sub tabular">Cat. ${cats.map(catNo).join(", ")}</span>` : ""}</th>
          <td class="num tabular ${s.kind === "less" || s.kind === "head" ? "text-2" : s.kind === "unexplained" && amt ? "is-neg strong" : "strong"}">${isLevel ? inr(amt) : amt ? (link ? html`<a href="${link}">${inr(-amt)}</a>` : inr(-amt)) : DASH}</td>
          <td class="v-itr-wf__barcol"><span class="v-itr-wf__track" aria-hidden="true"><span class="v-itr-wf__bar"></span></span></td>
          <td class="num tabular ${s.kind === "unexplained" && Math.abs(run) > 0.5 ? "is-neg strong" : ""}">${inr(run)}</td></tr>`;
      })}</tbody></table></div>`;
  }

  function deductionTable(deds) {
    return html`<div class="table-wrap"><table class="table table--compact v-itr-ded">
      <caption class="sr-only">Deduction candidates</caption>
      <thead><tr><th scope="col">Item</th><th scope="col" class="num">Paid</th><th scope="col" class="num">Cap</th><th scope="col" class="num">Eligible</th></tr></thead>
      <tbody>${deds.map((x) => html`<tr>
        <td><div class="small"><span class="mono xsmall text-2">${x.section}</span> ${x.title}</div>
          ${(x.transactions || []).length
            ? html`<details class="v-itr-details"><summary class="xsmall">${plural(x.tx_count ?? x.transactions.length, "transaction")}</summary>
                <ul class="v-itr-dedtx xsmall">${x.transactions.map((t) => html`<li><span class="tabular">${date(t.date)}</span> · <span class="tabular">${inr(t.amount)}</span> · ${t.narration}</li>`)}</ul></details>`
            : ""}</td>
        <td class="num tabular">${inr(x.amount)}</td>
        <td class="num tabular">${x.statutory_cap != null ? inr(x.statutory_cap, { decimals: 0 }) : html`<span class="muted">${DASH}</span>`}</td>
        <td class="num tabular ${x.eligible_upto_cap == null ? "muted" : "strong"}">${x.eligible_upto_cap != null ? inr(x.eligible_upto_cap) : x.section === "Not deductible" ? "Not deductible" : DASH}</td></tr>`)}</tbody></table></div>`;
  }

  function flagDetail(f) {
    const payees = Array.isArray(f.payees) ? f.payees : [];
    const occ = Array.isArray(f.occurrences) ? f.occurrences : [];
    const n = payees.length || occ.length;
    return html`<div class="v-itr-flag">
      <div class="row-wrap"><span class="badge badge--${sevTone(f.severity)}">${String(f.severity || "").toUpperCase() || "FLAG"}</span>
        ${f.verify_source ? html`<span class="badge badge--outline">Verify source</span>` : ""}
        <span class="small strong">${f.title}</span></div>
      ${f.detail ? html`<p class="xsmall text-2 v-itr-flag__detail">${f.detail}</p>` : ""}
      ${n
        ? html`<details class="v-itr-details">
            <summary class="small">${payees.length ? plural(payees.length, "payee") : plural(occ.length, "occurrence")}</summary>
            <div class="table-wrap"><table class="table table--compact">
              ${payees.length
                ? html`<thead><tr><th scope="col">Payee</th><th scope="col" class="num">Payments</th><th scope="col" class="num">FY total</th><th scope="col">Why flagged</th></tr></thead>
                  <tbody>${payees.map((p) => html`<tr>
                    <td><a href="${ctx.href("transactions", { q: p.payee, ...fyRange(data.financial_year) })}">${p.payee}</a></td>
                    <td class="num">${int(p.payment_count)}</td><td class="num">${inr(p.amount)}</td>
                    <td class="xsmall">${(p.reasons || []).join("; ")}</td></tr>`)}</tbody>`
                : html`<thead><tr><th scope="col">Date</th><th scope="col">Party</th><th scope="col" class="num">Amount</th></tr></thead>
                  <tbody>${occ.map((o) => html`<tr>
                    <td class="nowrap"><a href="${ctx.href("transactions", { date_from: o.date, date_to: o.date, channel: "" })}">${date(o.date)}</a></td>
                    <td>${o.person || o.party || html`<span class="muted">Not named</span>`}</td><td class="num">${inr(o.amount)}</td></tr>`)}</tbody>`}
            </table></div>
          </details>`
        : ""}
    </div>`;
  }

  const moneyRow = (label, section, v) =>
    html`<tr><td>${label}</td><td class="xsmall text-2">${section}</td><td class="num ${v ? "" : "muted"}">${v ? inr(v) : DASH}</td></tr>`;

  function drawSchedule(d, range) {
    const slot = body.querySelector('[data-slot="sched"]');
    const rows = d.category_schedule || [];
    schedGrid?.destroy();
    schedGrid = createDataGrid(slot, {
      ariaLabel: `Category schedule for ${d.financial_year}`,
      rowKey: "category_id",
      rows,
      height: "min(60vh, 520px)",
      rowHeight: 48,
      countNoun: ["category", "categories"],
      initialFilters: { active: "true" },
      initialSort: { key: "category_id", order: "asc" },
      filters: [
        { key: "q", type: "search", label: "Search categories", placeholder: "Category, group or section…" },
        { key: "active", type: "toggle", label: "With activity only" },
      ],
      clientFilter: (c, f) => {
        if (f.active && !(c.tx_count > 0)) return false;
        if (f.q) return `${c.category_name} ${c.group} ${c.itr_section}`.toLowerCase().includes(String(f.q).toLowerCase());
        return true;
      },
      onRowClick: (c) => ctx.navigate("transactions", { category_id: c.category_id, ...range }),
      emptyState: { icon: "table", title: "No categories with activity" },
      columns: [
        { key: "category_id", label: "#", width: "56px", align: "right", sortable: true, render: (c) => html`<span class="mono">${String(c.category_id).padStart(2, "0")}</span>` },
        { key: "category_name", label: "Category", width: "minmax(220px, 2fr)", sortable: true,
          render: (c) => html`<span class="strong">${c.category_name}</span><span class="dg__sub">${c.group}</span>` },
        { key: "itr_section", label: "ITR section", width: "minmax(160px, 1.2fr)", sortable: true,
          render: (c) => (c.itr_section ? html`<span class="badge badge--neutral">${c.itr_section}</span>` : html`<span class="muted">${DASH}</span>`) },
        { key: "credit", label: "Credits", width: "140px", align: "right", sortable: true, defaultOrder: "desc",
          className: (c) => (c.credit ? "is-credit" : "muted"), format: (v) => (v ? inr(v) : DASH) },
        { key: "debit", label: "Debits", width: "140px", align: "right", sortable: true, defaultOrder: "desc",
          className: (c) => (c.debit ? "is-debit" : "muted"), format: (v) => (v ? inr(v) : DASH) },
        { key: "tx_count", label: "Txns", width: "72px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => int(v) },
      ],
    });
    // meters: width from data-value via CSSOM (no inline style attributes under the CSP)
    body.querySelectorAll(".v-itr-meter .meter[data-value]").forEach((m) => setVars(m, { "--value": `${m.dataset.value}%` }));
    body.querySelectorAll(".v-itr-wf__row[data-w]").forEach((r) => setVars(r, { "--x0": `${r.dataset.x0}%`, "--w": `${r.dataset.w}%` }));
  }

  async function exportXlsx(btn) {
    if (!data) return;
    await ctx.withBusy(
      btn,
      async () => {
        const r = await ctx.api.download("export/itr-xlsx", `${ctx.caseId}_itr.xlsx`, { params: { fy: data.financial_year || fy || null } });
        ctx.mutated([]).catch(() => null); // audit-logged (EXPORT_ITR_XLSX)
        return r;
      },
      { success: (r) => `Saved ${r.filename} (${bytes(r.size)})`, error: "Excel export failed" }
    );
  }

  fySel.addEventListener("change", () => {
    fy = fySel.value;
    ctx.setParams({ fy: fy || null }, { push: true });
    load();
  });
  ctx.onCleanup(delegate(el, "click", "[data-action]", (e, b) => {
    if (b.dataset.action === "itr-retry") load();
    else if (b.dataset.action === "itr-xlsx") exportXlsx(b);
    else if (b.dataset.action === "itr-latest") {
      fy = "";
      ctx.setParams({ fy: null });
      load();
    }
  }));

  ctx.onData([TOPICS.transactions, TOPICS.suspense, TOPICS.statements, TOPICS.entities], () => load());
  ctx.registerCommands([
    { id: "itr.latest", title: "ITR: show latest financial year", group: "ITR", icon: "calendar", run: () => { fy = ""; ctx.setParams({ fy: null }); load(); } },
    { id: "itr.flags", title: "ITR: jump to statutory checks", group: "ITR", icon: "shield", run: () => el.querySelector("#itr-checks-h")?.scrollIntoView({ block: "start" }) },
    { id: "itr.xlsx", title: "ITR: export workbook (Excel)", group: "ITR", icon: "table", when: () => Boolean(data?.transaction_count), run: () => exportXlsx(null) },
  ]);

  await load();

  return {
    update(params) {
      const next = params.fy || "";
      if (next !== fy) {
        fy = next;
        load();
      }
    },
  };
}
