/* CypherEdge · Tally ledger map editor (opened from the Tally view; not a route).
 *
 * GET edge/tally-map -> { ledger_map, is_default }; save with POST edge/tally-map { ledger_map } (the server also
 * accepts PUT; a partial map is overlaid on the defaults and every validation problem comes back in one 400);
 * reset with { reset: true }. Map fields: company_name, voucher_prefix, use_trade_vouchers, sales_categories,
 * purchase_categories, category_groups {cid: group}, category_ledgers {cid: ledger}, counterparty_ledgers
 * {NORMALISED CP: {ledger, group|null}}, custom_groups {name: predefined parent}.
 * The stored map applies to the preview, export/tally-xml, export/tally-xlsx and edge/tally-push.
 */
import { delegate, html, render, uid } from "../core/dom.js";
import { catNo, plural } from "../core/format.js";
import { icon } from "../components/icons.js";
import { confirmDialog, openModal } from "../components/modal.js";
import { spinner } from "../components/states.js";

/** The 28 groups every Tally Prime / ERP 9 company starts with (edge/tally_exporter.PREDEFINED_TALLY_GROUPS). */
export const TALLY_PREDEFINED_GROUPS = [
  "Bank Accounts", "Bank OD A/c", "Branch / Divisions", "Capital Account", "Cash-in-Hand", "Current Assets",
  "Current Liabilities", "Deposits (Asset)", "Direct Expenses", "Direct Incomes", "Duties & Taxes", "Fixed Assets",
  "Indirect Expenses", "Indirect Incomes", "Investments", "Loans & Advances (Asset)", "Loans (Liability)",
  "Misc. Expenses (ASSET)", "Provisions", "Purchase Accounts", "Reserves & Surplus", "Sales Accounts",
  "Secured Loans", "Stock-in-Hand", "Sundry Creditors", "Sundry Debtors", "Suspense A/c", "Unsecured Loans",
];
/** Contra legs (bank / cash) and suspense: their group is fixed and they take no ledger override. */
const FIXED_GROUP = new Set([49, 50, 51]);
const NO_LEDGER = new Set([49, 50, 51, 52]);
const MAX_NAME = 64;
/** Same rule as the server (edge/tally_exporter): 1-12 of letters, digits, / _ - */
const PREFIX_RE = /^[A-Za-z0-9/_-]{1,12}$/;

const normCp = (s) => String(s || "").toUpperCase().replace(/\s+/g, " ").trim();

/**
 * Open the editor. Resolves to the saved { ledger_map, is_default } (or null when cancelled).
 * `categories` = store.categories(); `api` = ctx.api.
 */
export async function openTallyMapEditor({ api, categories, toast }) {
  // Show the dialog frame with a loading state at once (the map fetch can take a moment on a large case)
  let loadingDismissed = false;
  let fetched = false;
  const loading = openModal({
    title: "Tally ledger map",
    body: html`<div class="row" role="status">${spinner("Loading the ledger map")}<span class="small muted">Loading the ledger map…</span></div>`,
    actions: [{ label: "Cancel", value: "cancel" }],
  });
  loading.closed.then(() => {
    if (!fetched) loadingDismissed = true; // the analyst closed the dialog before the map arrived
  });
  let res;
  try {
    res = await api.get("edge/tally-map");
    fetched = true;
  } finally {
    loading.close(null);
  }
  await loading.closed;
  if (!res || loadingDismissed) return null;
  const map = res.ledger_map || {};
  const cats = (categories || []).slice().sort((a, b) => a.category_id - b.category_id);
  const salesOk = cats.filter((c) => !NO_LEDGER.has(c.category_id) && (c.typical_direction === "CR" || c.typical_direction === "BOTH"));
  const purchOk = cats.filter((c) => !NO_LEDGER.has(c.category_id) && (c.typical_direction === "DR" || c.typical_direction === "BOTH"));
  const sales = new Set((map.sales_categories || []).map(Number));
  const purch = new Set((map.purchase_categories || []).map(Number));
  const groups = map.category_groups || {};
  const ledgers = map.category_ledgers || {};
  const id = uid("tmap");

  const groupOptions = (cur, custom, { allowDefault = false } = {}) => {
    const all = [...TALLY_PREDEFINED_GROUPS, ...custom];
    const extra = cur && !all.includes(cur) ? [cur] : [];
    return html`${allowDefault ? html`<option value="">Category default</option>` : ""}
      <optgroup label="Predefined">${TALLY_PREDEFINED_GROUPS.map((g) => html`<option value="${g}" ${g === cur ? html`selected` : ""}>${g}</option>`)}</optgroup>
      ${custom.length || extra.length ? html`<optgroup label="Custom">${[...custom, ...extra].map((g) => html`<option value="${g}" ${g === cur ? html`selected` : ""}>${g}</option>`)}</optgroup>` : ""}`;
  };
  const customNames = (root) => [...root.querySelectorAll('[data-row="group"]')]
    .map((r) => r.querySelector('[data-f="name"]').value.trim())
    .filter(Boolean);

  const cgRow = (name = "", parent = "Indirect Expenses") => html`<div class="v-tally-map-row" data-row="group" role="group" aria-label="Custom group">
      <label class="field"><span class="field__label">Group name</span>
        <input class="input input--sm" data-f="name" maxlength="${MAX_NAME}" value="${name}" autocomplete="off" required></label>
      <label class="field"><span class="field__label">Under</span>
        <select class="select select--sm" data-f="parent">${TALLY_PREDEFINED_GROUPS.map((g) => html`<option value="${g}" ${g === parent ? html`selected` : ""}>${g}</option>`)}</select></label>
      <button type="button" class="btn btn--sm btn--ghost btn--icon" data-action="row-remove" aria-label="Remove custom group" title="Remove">${icon("trash", { size: "sm" })}</button>
    </div>`;
  const cpRow = (cp = "", ledger = "", group = "", custom = []) => html`<div class="v-tally-map-row v-tally-map-row--cp" data-row="cp" role="group" aria-label="Counterparty ledger">
      <label class="field"><span class="field__label">Counterparty (as in narration)</span>
        <input class="input input--sm" data-f="cp" maxlength="120" value="${cp}" autocomplete="off" spellcheck="false" required></label>
      <label class="field"><span class="field__label">Ledger</span>
        <input class="input input--sm" data-f="ledger" maxlength="${MAX_NAME}" value="${ledger}" autocomplete="off" required></label>
      <label class="field"><span class="field__label">Group</span>
        <select class="select select--sm" data-f="group" data-groupsel="default">${groupOptions(group, custom, { allowDefault: true })}</select></label>
      <button type="button" class="btn btn--sm btn--ghost btn--icon" data-action="row-remove" aria-label="Remove counterparty ledger" title="Remove">${icon("trash", { size: "sm" })}</button>
    </div>`;

  const custom0 = Object.keys(map.custom_groups || {});
  const body = html`<div class="stack-lg v-tally-map">
    ${res.is_default ? html`<div class="callout callout--info">${icon("info")}<div>This case uses the default map. Changes are saved for this case only and are audit-logged.</div></div>` : ""}
    <section class="stack-sm" aria-labelledby="${id}-def">
      <h3 class="subhead" id="${id}-def">Defaults</h3>
      <div class="form-grid">
        <label class="field" for="${id}-company"><span class="field__label">Tally company name</span>
          <input class="input" id="${id}-company" data-f="company_name" maxlength="120" autocomplete="off" value="${map.company_name || ""}" placeholder="Company open in Tally">
          <span class="field__hint">Used when the Tally page leaves the company blank</span></label>
        <label class="field" for="${id}-prefix"><span class="field__label">Voucher number prefix</span>
          <input class="input input--w-sm" id="${id}-prefix" data-f="voucher_prefix" maxlength="12" pattern="[A-Za-z0-9\\/_\\-]{1,12}" required value="${map.voucher_prefix || "CS"}" aria-describedby="${id}-prefix-hint">
          <span class="field__hint" id="${id}-prefix-hint" data-slot="prefix-hint">1–12 letters, digits, / _ -</span></label>
        <label class="check v-tally-map-check"><input type="checkbox" data-f="use_trade_vouchers" ${map.use_trade_vouchers ? html`checked` : ""}>
          <span>Use Sales / Purchase vouchers by default</span></label>
      </div>
    </section>
    <section class="stack-sm" aria-labelledby="${id}-trade">
      <h3 class="subhead" id="${id}-trade">Sales / Purchase categories</h3>
      <p class="xsmall muted">With Sales / Purchase vouchers on, credits in these categories become Sales vouchers and debits Purchase vouchers; everything else stays Receipt / Payment.</p>
      <div class="grid-2">
        <fieldset class="field"><legend class="field__label">Sales (credits)</legend>
          <div class="v-tally-map-checks">${salesOk.map((c) => html`<label class="check"><input type="checkbox" data-f="sales" value="${c.category_id}" ${sales.has(c.category_id) ? html`checked` : ""}><span>${catNo(c.category_id)} · ${c.name}</span></label>`)}</div></fieldset>
        <fieldset class="field"><legend class="field__label">Purchase (debits)</legend>
          <div class="v-tally-map-checks">${purchOk.map((c) => html`<label class="check"><input type="checkbox" data-f="purchase" value="${c.category_id}" ${purch.has(c.category_id) ? html`checked` : ""}><span>${catNo(c.category_id)} · ${c.name}</span></label>`)}</div></fieldset>
      </div>
    </section>
    <section class="stack-sm" aria-labelledby="${id}-groups">
      <div class="row-between"><h3 class="subhead" id="${id}-groups">Custom groups</h3>
        <button type="button" class="btn btn--sm" data-action="add-group">${icon("plus", { size: "sm" })}Add group</button></div>
      <p class="xsmall muted">Groups that do not exist in a new Tally company; each is emitted as a GROUP master under a predefined parent.</p>
      <div class="stack-sm" data-slot="groups">${Object.entries(map.custom_groups || {}).map(([n, pg]) => cgRow(n, pg))}</div>
    </section>
    <section class="stack-sm" aria-labelledby="${id}-cats">
      <h3 class="subhead" id="${id}-cats">Category → group and ledger</h3>
      <p class="xsmall muted">Leave the ledger blank to use the counterparty (party) ledger. Bank, cash and suspense rows are fixed.</p>
      <div class="table-wrap table-wrap--max"><table class="table table--compact v-tally-map-table">
        <caption class="sr-only">Tally group and ledger for each transaction category</caption>
        <thead><tr><th scope="col">Category</th><th scope="col">Group</th><th scope="col">Ledger override</th></tr></thead>
        <tbody>${cats.map((c) => {
          const cid = c.category_id;
          const g = groups[String(cid)] || c.tally_parent_group || "";
          return html`<tr data-cat="${cid}">
            <th scope="row" class="small"><span class="tabular muted">${catNo(cid)}</span> ${c.name}</th>
            <td><select class="select select--sm" data-f="group" data-groupsel="1" aria-label="Group for ${c.name}" ${FIXED_GROUP.has(cid) ? html`disabled` : ""}>${groupOptions(g, custom0)}</select></td>
            <td><input class="input input--sm" data-f="ledger" maxlength="${MAX_NAME}" aria-label="Ledger for ${c.name}" value="${ledgers[String(cid)] || ""}"
              placeholder="${NO_LEDGER.has(cid) ? "Fixed" : "Party ledger"}" ${NO_LEDGER.has(cid) ? html`disabled` : ""} autocomplete="off"></td></tr>`;
        })}</tbody></table></div>
    </section>
    <section class="stack-sm" aria-labelledby="${id}-cps">
      <div class="row-between"><h3 class="subhead" id="${id}-cps">Counterparty ledgers</h3>
        <button type="button" class="btn btn--sm" data-action="add-cp">${icon("plus", { size: "sm" })}Add counterparty</button></div>
      <p class="xsmall muted">Post a counterparty to a fixed ledger (and optionally a group), whatever its category. Names are matched case-insensitively.</p>
      <div class="stack-sm" data-slot="cps">${Object.entries(map.counterparty_ledgers || {}).map(([cp, v]) => cpRow(cp, v?.ledger || "", v?.group || "", custom0))}</div>
    </section>
    <div class="row"><span class="spacer"></span>
      <button type="button" class="btn btn--sm btn--danger" data-action="map-reset" ${res.is_default ? html`disabled` : ""}>${icon("refresh", { size: "sm" })}Reset to defaults</button></div>
  </div>`;

  let saved = null;
  const m = openModal({
    title: "Tally ledger map",
    description: "How categories and counterparties become Tally groups, ledgers and voucher types for this case.",
    size: "xl",
    body,
    actions: [{ label: "Cancel", value: "cancel" }, { label: "Save map", variant: "primary", type: "submit" }],
    onSubmit: async (values, { el: dlg }) => {
      const prefixEl = dlg.querySelector('[data-f="voucher_prefix"]');
      const prefix = prefixEl.value.trim();
      const hint = dlg.querySelector('[data-slot="prefix-hint"]');
      if (prefix && !PREFIX_RE.test(prefix)) {
        prefixEl.setAttribute("aria-invalid", "true");
        hint.textContent = "Use 1–12 letters, digits, / _ or - (no spaces or other symbols).";
        hint.classList.add("field__error");
        prefixEl.focus();
        return false;
      }
      prefixEl.removeAttribute("aria-invalid");
      hint.textContent = "1–12 letters, digits, / _ -";
      hint.classList.remove("field__error");
      const ledger_map = collect(dlg);
      const out = await api.post("edge/tally-map", { ledger_map });
      saved = out;
      toast?.success?.("Tally ledger map saved");
      return out;
    },
  });
  const dlg = m.el;

  /** Keep every group select in step with the custom group names. */
  function refreshGroupSelects() {
    const custom = customNames(dlg);
    dlg.querySelectorAll("select[data-groupsel]").forEach((sel) => {
      const cur = sel.value;
      render(sel, groupOptions(cur, custom, { allowDefault: sel.dataset.groupsel === "default" }));
      sel.value = cur;
    });
  }

  function collect(root) {
    const v = (sel) => root.querySelector(sel);
    const out = {
      company_name: v('[data-f="company_name"]').value.trim() || null,
      voucher_prefix: v('[data-f="voucher_prefix"]').value.trim() || "CS",
      use_trade_vouchers: v('[data-f="use_trade_vouchers"]').checked,
      sales_categories: [...root.querySelectorAll('[data-f="sales"]:checked')].map((x) => Number(x.value)),
      purchase_categories: [...root.querySelectorAll('[data-f="purchase"]:checked')].map((x) => Number(x.value)),
      custom_groups: {},
      category_groups: {},
      category_ledgers: {},
      counterparty_ledgers: {},
    };
    for (const r of root.querySelectorAll('[data-row="group"]')) {
      const n = r.querySelector('[data-f="name"]').value.trim();
      if (n) out.custom_groups[n] = r.querySelector('[data-f="parent"]').value;
    }
    for (const r of root.querySelectorAll("tr[data-cat]")) {
      const cid = r.dataset.cat;
      const g = r.querySelector('[data-f="group"]');
      const l = r.querySelector('[data-f="ledger"]');
      if (g && !g.disabled && g.value) out.category_groups[cid] = g.value;
      if (l && !l.disabled && l.value.trim()) out.category_ledgers[cid] = l.value.trim();
    }
    for (const r of root.querySelectorAll('[data-row="cp"]')) {
      const cp = normCp(r.querySelector('[data-f="cp"]').value);
      const ledger = r.querySelector('[data-f="ledger"]').value.trim();
      if (!cp && !ledger) continue;
      if (!cp || !ledger) throw new Error("Every counterparty row needs both a counterparty and a ledger name.");
      out.counterparty_ledgers[cp] = { ledger, group: r.querySelector('[data-f="group"]').value || null };
    }
    return out;
  }

  const offs = [
    delegate(dlg, "click", "[data-action]", async (e, b) => {
      const a = b.dataset.action;
      if (a === "add-group") {
        const host = dlg.querySelector('[data-slot="groups"]');
        host.insertAdjacentHTML("beforeend", String(cgRow("", "Indirect Expenses")));
        host.lastElementChild.querySelector("input")?.focus();
      } else if (a === "add-cp") {
        const host = dlg.querySelector('[data-slot="cps"]');
        host.insertAdjacentHTML("beforeend", String(cpRow("", "", "", customNames(dlg))));
        host.lastElementChild.querySelector("input")?.focus();
      } else if (a === "row-remove") {
        const row = b.closest("[data-row]");
        const next = row?.nextElementSibling || row?.previousElementSibling;
        const wasGroup = row?.dataset.row === "group";
        row?.remove();
        (next?.querySelector("input") || dlg.querySelector('[data-action="add-cp"]'))?.focus();
        if (wasGroup) refreshGroupSelects();
      } else if (a === "map-reset") {
        const ok = await confirmDialog({
          title: "Reset the Tally ledger map?",
          message: "Every custom group, ledger override, counterparty ledger and default for this case goes back to the built-in map. This is audit-logged.",
          confirmLabel: "Reset map",
          danger: true,
        });
        if (!ok || !dlg.isConnected) return;
        try {
          saved = await api.post("edge/tally-map", { reset: true });
          toast?.success?.("Tally ledger map reset to defaults");
          m.close(saved);
        } catch (err) {
          toast?.error?.(err, { title: "Could not reset the map" });
        }
      }
    }),
    delegate(dlg, "change", '[data-row="group"] [data-f="name"]', () => refreshGroupSelects()),
  ];
  const result = await m.closed;
  offs.forEach((off) => off());
  return result && result.ledger_map ? result : saved;
}

export const describeMapSummary = (lm) => {
  if (!lm) return "";
  const parts = [];
  const cg = Object.keys(lm.custom_groups || {}).length;
  const cl = Object.keys(lm.category_ledgers || {}).length;
  const cp = Object.keys(lm.counterparty_ledgers || {}).length;
  if (cg) parts.push(plural(cg, "custom group"));
  if (cl) parts.push(plural(cl, "category ledger"));
  if (cp) parts.push(plural(cp, "counterparty ledger"));
  return parts.join(" · ");
};
