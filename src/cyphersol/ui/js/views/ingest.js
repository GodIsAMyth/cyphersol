/* Ingest (#/case/{id}/ingest): statement upload queue, custody table + provenance drawer, PDF Draw Tool.
 *
 * Upload tab
 *   - drag-and-drop / picker queue (UPLOAD_ACCEPT), per-file status chips (queued → reading → uploading →
 *     ok | failed | duplicate), a password per PDF (plus "apply to all PDFs") and holder details per PDF
 *     (name, DOB, PAN, account, customer ID, mobile, bank, extra passwords; PDF-13) tried against the file through
 *     POST /api/pdf/password-check, which reports only the formula that worked. Details and passwords live in
 *     this tab's memory only, go out as files[i].password_hints, and are dropped once the file is ingested.
 *   - per-batch execution-mode
 *     override (does NOT change the global mode — Settings is the single source of truth), draw-template
 *     override, duplicate policy. A 409 (same SHA-256 already in the case) marks the named files and offers
 *     "Skip duplicates & ingest the rest" (on_duplicate: "skip").
 *   - per-file results from the ingest report: rows, bank/account, date order, sheets, OCR corrections,
 *     missing metadata, statement flags, anomaly-gate checks (opening/closing balance, zero/dual amounts,
 *     rows outside the period, ...), file-name cross-checks, period coverage, OCR confidence per page, PAN,
 *     password formula, warnings (details drawer).
 *   - last pipeline telemetry + "Run AI backup agents (S2–S4)"; ingest history.
 *   Each file is staged with its own request (POST …/uploads), then a background job ingests the batch
 *   (POST …/ingest-jobs → 202) and is polled (GET /api/jobs/{id}) for phase / progress shown on the chips (P2).
 *   Uploads are NOT bound to the view's AbortSignal: the server finishes the job even if the analyst
 *   navigates away, so the result is still applied to the store and reported with a toast.
 * Statements tab: custody grid (bank, period, rows, opening/closing, balance check, full SHA-256) and a
 *   provenance drawer per statement (GET …/provenance).
 * Draw tool tab: POST …/pdf-preview (text tokens) + POST /api/draw/page-image (page raster under the overlay,
 *   PDF-21), SVG page where dragging on the page draws a new table frame, draggable / keyboard-adjustable
 *   frame edges and column dividers, field per column (incl. value date, signed amount, Dr/Cr marker), live
 *   parsed-row preview that anchors right-aligned amounts by their right edge like the parser (PDF-23),
 *   POST /api/draw-templates. */
import { ApiError, api, fileToBase64, isAbort } from "../core/api.js";
import { EXEC_MODES, UPLOAD_ACCEPT, execModeLabel } from "../core/constants.js";
import { delegate, formValues, html, render, uid } from "../core/dom.js";
import { DASH, bytes, date, dateTime, inr, int, plural } from "../core/format.js";
import { describeAgentTelemetry, describeUploadResult } from "../core/results.js";
import { MUTATION_TOPICS, TOPICS, store as appStore } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { openDrawer } from "../components/drawer.js";
import { icon } from "../components/icons.js";
import { confirmDialog, openModal } from "../components/modal.js";
import { emptyState, errorState, pageHead, spinner } from "../components/states.js";
import { createTabs } from "../components/tabs.js";
import { toast, withBusy } from "../components/toast.js";

export const styles = ["views/cases.css"];

const ACCEPT_EXT = new Set(UPLOAD_ACCEPT.split(",").map((s) => s.trim().replace(/^\./, "").toLowerCase()));
const MAX_BATCH_BYTES = 90 * 1024 * 1024; // raw bytes per ingest job (files are staged one request each)

const STATUS = {
  queued: { label: "Queued", tone: "neutral" },
  reading: { label: "Reading", tone: "info", busy: true },
  uploading: { label: "Processing", tone: "info", busy: true },
  ok: { label: "Ingested", tone: "success" },
  empty: { label: "0 rows", tone: "warn" },
  failed: { label: "Failed", tone: "danger" },
  conflict: { label: "Already in case", tone: "warn" },
  duplicate: { label: "Skipped (duplicate)", tone: "warn" },
};
const SENDABLE = new Set(["queued", "failed", "conflict"]);

const COLUMN_FIELDS = [
  { value: "date", label: "Txn date" },
  { value: "value_date", label: "Value date" },
  { value: "narration", label: "Narration" },
  { value: "ref", label: "Ref / cheque / UTR" },
  { value: "debit", label: "Debit" },
  { value: "credit", label: "Credit" },
  { value: "amount", label: "Amount (signed)" },
  { value: "type", label: "Dr/Cr marker" },
  { value: "balance", label: "Balance" },
  { value: "ignore", label: "Ignore" },
];
const DRAW_DEFAULTS = {
  bbox: [0.02, 0.12, 0.98, 0.96],
  splits: [0.15, 0.51, 0.64, 0.75, 0.87],
  names: ["date", "narration", "ref", "debit", "credit", "balance"],
};
const MIN_GAP = 0.01;
const MIN_FRAME = 0.05;
const DRAW_DPI = 110;
// Same pattern as pdf_parser._RIGHT_ALIGNED_NUMERIC_RE: such tokens are placed by their right edge (PDF-23)
const RIGHT_NUMERIC = /^[(\-\s₹]*\d[\d,]*(?:\.\d{1,2})?(?:\s*(?:CR|DR))?[)\s]*$/i;

/** Holder details the server's password generator understands (subset of /api/meta password_hint_keys). */
const HINT_FIELDS = [
  { key: "name", label: "Account holder name", ph: "As printed on the statement" },
  { key: "dob", label: "Date of birth", type: "date" },
  { key: "pan", label: "PAN", ph: "ABCDE1234F", max: 10 },
  { key: "account_number", label: "Account number", ph: "Digits only", mode: "numeric" },
  { key: "customer_id", label: "Customer ID / CRN / CIF", ph: "As issued by the bank" },
  { key: "mobile", label: "Registered mobile", ph: "10 digits", mode: "tel" },
  { key: "bank", label: "Bank", ph: "e.g. SBI, HDFC, ICICI" },
  { key: "passwords", label: "Other passwords to try", ph: "One per line (max 32)", multi: true },
];
const BANK_WORDS = new Set(["SBI", "HDFC", "ICICI", "AXIS", "KOTAK", "PNB", "YES", "INDUS", "INDUSIND", "BOB", "CANARA", "UNION", "IDFC", "IDBI", "BOI", "CBI", "FEDERAL", "RBL", "AU", "BANDHAN", "COSMOS", "SARASWAT", "UCO", "IOB"]);
const PWD_PARTS = {
  dob_ddmmyyyy: "DOB as DDMMYYYY", dob_ddmmyy: "DOB as DDMMYY", dob_yyyymmdd: "DOB as YYYYMMDD", dob_ddmm: "DOB as DDMM",
  dob_yyyy: "birth year", dob_ddmonyyyy_upper: "DOB as DDMONYYYY", dob_ddmonyyyy_lower: "DOB as DDmonYYYY",
  pan_upper: "PAN (upper case)", pan_lower: "PAN (lower case)", name4_lower: "first 4 letters of name (lower)",
  name4_upper: "first 4 letters of name (upper)", name4_title: "first 4 letters of name (Title)",
};
/** "name4_lower+dob_ddmm" -> "first 4 letters of name (lower) + DOB as DDMM"; never shows a password. */
function passwordFormula(label) {
  if (!label) return DASH;
  if (label === "supplied") return "the password typed for this file";
  if (/^supplied_\d+$/.test(label)) return `listed password #${label.split("_")[1]}`;
  return label
    .split("+")
    .map((p) => PWD_PARTS[p] || p.replace(/_/g, " "))
    .join(" + ");
}
function cleanHints(h) {
  const out = {};
  if (!h) return out;
  for (const f of HINT_FIELDS) {
    const v = h[f.key];
    if (f.multi) {
      const list = String(v || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean).slice(0, 32);
      if (list.length) out[f.key] = list;
    } else if (String(v || "").trim()) out[f.key] = String(v).trim();
  }
  return out;
}
const hasHints = (h) => Boolean(h) && Object.keys(h).length > 0;
/** Holder name, bank and account guessed from names like CASE041_POOJAN-VIG_HDFC_501002938471.pdf. */
function hintsFromFilename(name) {
  const out = {};
  const parts = String(name).replace(/\.[^.]+$/, "").split(/[_\s]+/).filter(Boolean);
  for (const p of parts) {
    const up = p.toUpperCase();
    if (/^\d{8,18}$/.test(p) && !out.account_number) out.account_number = p;
    else if (BANK_WORDS.has(up) && !out.bank) out.bank = up;
    else if (/^[A-Za-z]+(?:-[A-Za-z]+)+$/.test(p) && !out.name) out.name = p.replace(/-/g, " ");
  }
  return out;
}

const extOf = (name) => (String(name).includes(".") ? String(name).split(".").pop().toLowerCase() : "");
const statusChip = (st, text) => {
  const s = STATUS[st] || STATUS.queued;
  return html`<span class="badge badge--${s.tone} v-ingest-chip" data-status="${st}">${s.busy ? spinner(s.label) : ""}${text || s.label}</span>`;
};
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const JOB_PHASES = { parse: "Parsing", validate: "Validating", entities: "Resolving entities", commit: "Saving" };
/** Chip text for a running ingest job: "Parsing 12/100 · 34%". */
const jobLabel = (j) => {
  if (j.status === "queued") return "Waiting to start";
  const phase = JOB_PHASES[j.phase] || "Processing";
  const count = j.total > 1 ? ` ${j.done}/${j.total}` : "";
  return `${phase}${count} · ${Math.round((j.progress || 0) * 100)}%`;
};
const pct1 = (f) => (Math.round(f * 1000) / 10).toString();

const pageList = (pages) => (pages || []).map(Number).sort((a, b) => a - b).join(", ");

/**
 * Anomaly-gate and cross-check findings of one ingest report (ING-10/11/16/17, PDF-10, FR-ING-01):
 * [{tone: danger|warn|info, short, label, detail?}]. Used by the queue badges and the details drawer.
 */
function gateFindings(r) {
  const a = (r && r.anomalies) || {};
  const n = (k) => Number(a[k]) || 0;
  const out = [];
  const add = (tone, short, label, detail) => out.push({ tone, short, label, detail });
  if (n("balance_mismatch")) add("danger", `${int(n("balance_mismatch"))} mismatch`, `${plural(n("balance_mismatch"), "row")} break the running balance`);
  if (n("opening_balance_mismatch")) add("danger", "Opening ≠ row 1", "Stated opening balance does not reconcile with the first row");
  if (n("closing_balance_mismatch")) add("danger", "Closing ≠ last row", "Stated closing balance differs from the last row's balance");
  if (a.balance_check_skipped === true) add("warn", "Balance not checked", "No running-balance column, so continuity could not be checked");
  if (n("date_unparseable")) add("danger", `${int(n("date_unparseable"))} bad date`, `${plural(n("date_unparseable"), "row")} with an unparseable date`);
  if (n("zero_or_dual_amount")) add("warn", `${int(n("zero_or_dual_amount"))} zero/dual`, `${plural(n("zero_or_dual_amount"), "row")} with a zero amount or both debit and credit`);
  if (n("date_out_of_order")) add("warn", `${int(n("date_out_of_order"))} out of order`, `${plural(n("date_out_of_order"), "row")} dated before the previous row`);
  if (n("duplicate_suspect")) add("warn", `${int(n("duplicate_suspect"))} dup`, `${plural(n("duplicate_suspect"), "duplicate suspect")} (same account, date, amount, direction and narration)`);
  if (n("date_order_ambiguous")) add("warn", "Date order?", `${plural(n("date_order_ambiguous"), "row")} where DD/MM vs MM/DD could not be decided`);
  if (n("outside_statement_period")) add("warn", `${int(n("outside_statement_period"))} outside period`, `${plural(n("outside_statement_period"), "row")} dated outside the stated statement period`);
  if (n("direction_inferred")) add("info", "Direction inferred", `${plural(n("direction_inferred"), "row")} whose debit/credit side was inferred from the balance`);
  if (n("ocr_autocorrected")) add("info", "OCR self-healed", `${plural(n("ocr_autocorrected"), "row")} whose figures OCR self-healing changed (before/after values in the audit log)`);
  if (n("llm_repaired")) add("info", "LLM repaired", `${plural(n("llm_repaired"), "row")} repaired by the AI agent (original values kept)`);
  const low = r.low_confidence_pages || [];
  if (low.length) add("warn", `Low OCR p. ${pageList(low)}`, `Low OCR confidence on page(s) ${pageList(low)}`);
  else if (r.low_ocr_confidence) add("warn", "Low OCR confidence", "Low OCR confidence on this statement");
  const rej = r.ocr_rejected_pages || [];
  if (rej.length) add("danger", `OCR rejected p. ${pageList(rej)}`, `OCR output rejected on page(s) ${pageList(rej)}; rows from them are missing`);
  const fx = r.filename_crosscheck || {};
  const conflicts = ["bank", "account", "holder", "period"].filter((k) => fx[k] === "conflict");
  if (conflicts.length) add("danger", `File name ≠ ${conflicts.join("/")}`, `The file name disagrees with the statement header on: ${conflicts.join(", ")}`);
  const pc = r.period_coverage || {};
  if (Number(pc.rows_outside) > 0 && !n("outside_statement_period")) add("warn", `${int(pc.rows_outside)} outside period`, `${plural(Number(pc.rows_outside), "row")} outside the stated period`);
  const gapS = Number(pc.days_without_rows_at_start) || 0;
  const gapE = Number(pc.days_without_rows_at_end) || 0;
  if (gapS >= 7 || gapE >= 7) {
    add("info", "Period gap", `No rows for ${[gapS >= 7 ? `the first ${plural(gapS, "day")}` : "", gapE >= 7 ? `the last ${plural(gapE, "day")}` : ""].filter(Boolean).join(" and ")} of the stated period`);
  }
  return out;
}

function pwdCheckBadge(c) {
  if (!c) return "";
  if (c.status === "ok") {
    return html`<span class="badge badge--success" title="Opens with: ${passwordFormula(c.label)}">${icon("key", { size: "sm" })}Unlocks · ${passwordFormula(c.label)}</span>`;
  }
  if (c.status === "no_match") return html`<span class="badge badge--warn">No candidate matched (${int(c.candidates)} tried)</span>`;
  if (c.status === "not_encrypted") return html`<span class="badge badge--neutral">Not password-protected</span>`;
  return html`<span class="badge badge--warn">Cannot test: no PDF decryption engine</span>`;
}

export async function mount(el, ctx) {
  const data = await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return;
  const { store } = ctx;
  const caseId = ctx.caseId;

  // ===================================================================================== state
  const queue = []; // {id, file, name, size, ext, isPdf, password, status, error, report}
  const cfg0 = store.get().llmConfig || {};
  let batchMode = ["heuristics", "both", "llm"].includes(cfg0.execution_mode) ? cfg0.execution_mode : "heuristics";
  let templateId = "";
  let skipDuplicates = false;
  let running = false;
  let lastTelemetry = data.latest_agent_telemetry || null;
  let provenance = null; // { provenance, ingest_results } cache
  let provenancePromise = null;

  // ===================================================================================== shell
  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Ingest statements",
        sub: html`Upload, verify chain of custody and calibrate PDF layouts for <span class="strong">${(ctx.case && ctx.case.title) || caseId}</span>.`,
      })}
      <div data-slot="tabs"></div>
    </div>`
  );
  const tabs = createTabs(el.querySelector('[data-slot="tabs"]'), {
    label: "Ingest sections",
    tabs: [
      { id: "upload", label: "Upload", icon: "upload" },
      { id: "statements", label: "Statements & custody", icon: "hash", badge: int((data.statements || []).length) },
      { id: "draw", label: "PDF draw tool", icon: "table" },
    ],
    selected: ["upload", "statements", "draw"].includes(ctx.params.tab) ? ctx.params.tab : "upload",
    fireInitial: false,
    onChange: (id, panel) => {
      ensurePanel(id, panel);
      if (ctx.isCurrent()) ctx.setParams({ tab: id === "upload" ? null : id });
    },
  });

  const built = new Set();
  function ensurePanel(id, panel) {
    if (built.has(id)) return;
    built.add(id);
    if (id === "upload") buildUpload(panel);
    else if (id === "statements") buildStatements(panel);
    else if (id === "draw") buildDraw(panel);
  }

  // ===================================================================================== UPLOAD
  let up = null; // element refs for the upload panel
  function buildUpload(panel) {
    const inputId = uid("ing-file");
    render(
      panel,
      html`<div class="v-ingest-layout">
        <div class="stack min-w-0">
          <section class="card" aria-labelledby="ing-q-title">
            <div class="card__head">
              <div><h2 class="card__title" id="ing-q-title">Statement files</h2>
                <p class="card__sub">PDF, Excel (.xlsx/.xlsm/.xls), CSV/TSV/TXT and scanned images. Each file is hashed (SHA-256) on ingest.</p></div>
            </div>
            <div class="card__body stack">
              <div class="v-ingest-drop" data-drop>
                <input type="file" id="${inputId}" class="sr-only" multiple accept="${UPLOAD_ACCEPT}" tabindex="-1" aria-hidden="true" data-file-input />
                <span class="v-ingest-drop__icon">${icon("upload", { size: "lg" })}</span>
                <div class="v-ingest-drop__text">
                  <span class="strong">Drop statement files here</span>
                  <span class="small muted">or</span>
                  <button type="button" class="btn btn--sm" data-action="pick">${icon("folder", { size: "sm" })}Choose files…</button>
                </div>
                <span class="xsmall muted">${UPLOAD_ACCEPT.replace(/,/g, " ")}</span>
              </div>
              <div data-slot="queue"></div>
              <div class="row-wrap v-ingest-actions">
                <button type="button" class="btn btn--primary" data-action="ingest" disabled>${icon("play", { size: "sm" })}<span data-slot="ingest-label">Ingest files</span></button>
                <button type="button" class="btn btn--ghost" data-action="clear-done" hidden>Clear finished</button>
                <button type="button" class="btn btn--ghost" data-action="clear-all" hidden>Clear queue</button>
                <span class="spacer"></span>
                <span class="small muted" data-slot="queue-summary" aria-live="polite"></span>
              </div>
              <div data-slot="result" aria-live="polite"></div>
            </div>
          </section>

          <section class="card" aria-labelledby="ing-tel-title">
            <div class="card__head">
              <div><h2 class="card__title" id="ing-tel-title">Last pipeline run</h2>
                <p class="card__sub">Which AI stages (S1–S4) were invoked; heuristics handle everything else locally.</p></div>
              <button type="button" class="btn btn--sm" data-action="run-agents">${icon("cpu", { size: "sm" })}Run AI backup agents (S2–S4)</button>
            </div>
            <div class="card__body" data-slot="telemetry"></div>
          </section>

          <section class="card" aria-labelledby="ing-hist-title">
            <div class="card__head">
              <div><h2 class="card__title" id="ing-hist-title">Ingest history</h2>
                <p class="card__sub">Per-file outcomes recorded for this case, including failures and skipped duplicates.</p></div>
            </div>
            <div class="card__body" data-slot="history"></div>
          </section>
        </div>

        <aside class="stack min-w-0" aria-label="Batch options">
          <section class="card" aria-labelledby="ing-opt-title">
            <div class="card__head"><h2 class="card__title" id="ing-opt-title">Batch options</h2></div>
            <div class="card__body stack">
              <fieldset class="v-ingest-fieldset">
                <legend class="field__label">Execution mode for this batch</legend>
                <div class="v-ingest-modes" role="radiogroup" aria-describedby="ing-mode-hint">
                  ${EXEC_MODES.map(
                    (m) => html`<label class="v-ingest-mode">
                      <input type="radio" name="ing-exec-mode" value="${m.value}" ${m.value === batchMode ? html`checked` : ""} />
                      <span><span class="strong">${m.label}</span><span class="xsmall muted">${m.desc}</span></span>
                    </label>`
                  )}
                </div>
                <span class="field__hint" id="ing-mode-hint" data-slot="mode-hint"></span>
              </fieldset>
              <div data-slot="mode-warn"></div>
              <div class="field">
                <label class="field__label" for="ing-template">PDF layout template</label>
                <select class="select" id="ing-template" data-slot="template"></select>
                <span class="field__hint">Auto-detect covers the built-in bank layouts. Calibrate a custom one in the PDF draw tool.</span>
              </div>
              <div class="field">
                <span class="field__label" id="ing-dup-label">Duplicate files</span>
                <div class="row">
                  <button type="button" class="switch" role="switch" aria-checked="false" aria-labelledby="ing-dup-label" aria-describedby="ing-dup-hint" data-action="toggle-skip"></button>
                  <span class="small text-2">Skip files already in this case</span>
                </div>
                <span class="field__hint" id="ing-dup-hint">Off: a re-upload of the same file (same SHA-256) stops the batch so you can decide.</span>
              </div>
              <div class="field">
                <label class="field__label" for="ing-pwd-all">Password for all PDFs</label>
                <div class="row">
                  <input class="input" type="password" id="ing-pwd-all" autocomplete="off" placeholder="DOB / PAN if encrypted" />
                  <button type="button" class="btn" data-action="apply-pwd">Apply</button>
                </div>
                <span class="field__hint">Fills every PDF in the queue; individual passwords can be set per file.</span>
              </div>
            </div>
          </section>
        </aside>
      </div>`
    );
    up = {
      panel,
      input: panel.querySelector("[data-file-input]"),
      drop: panel.querySelector("[data-drop]"),
      queue: panel.querySelector('[data-slot="queue"]'),
      ingest: panel.querySelector('[data-action="ingest"]'),
      clearDone: panel.querySelector('[data-action="clear-done"]'),
      clearAll: panel.querySelector('[data-action="clear-all"]'),
      result: panel.querySelector('[data-slot="result"]'),
      template: panel.querySelector('[data-slot="template"]'),
    };

    // ---- file picking / drag & drop
    const offs = [
      delegate(panel, "click", '[data-action="pick"]', () => up.input.click()),
      delegate(panel, "change", "[data-file-input]", () => {
        addFiles(up.input.files);
        up.input.value = "";
      }),
    ];
    let depth = 0;
    const dragOn = (on) => up.drop.classList.toggle("is-dragover", on);
    up.drop.addEventListener("dragenter", (e) => {
      e.preventDefault();
      depth += 1;
      dragOn(true);
    });
    up.drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    });
    up.drop.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (!depth) dragOn(false);
    });
    up.drop.addEventListener("drop", (e) => {
      e.preventDefault();
      depth = 0;
      dragOn(false);
      addFiles(e.dataTransfer?.files);
    });
    // stop a missed drop from navigating the SPA to the file
    const guard = (e) => {
      if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) e.preventDefault();
    };
    window.addEventListener("dragover", guard);
    window.addEventListener("drop", guard);
    ctx.onCleanup(() => {
      window.removeEventListener("dragover", guard);
      window.removeEventListener("drop", guard);
    });

    offs.push(
      delegate(panel, "click", '[data-action="ingest"]', () => runIngest()),
      delegate(panel, "click", '[data-action="skip-retry"]', () => runIngest({ forceSkip: true })),
      delegate(panel, "click", '[data-action="drop-conflicts"]', () => {
        removeWhere((q) => q.status === "conflict");
        render(up.result, "");
      }),
      delegate(panel, "click", '[data-action="clear-done"]', () => removeWhere((q) => ["ok", "empty", "duplicate"].includes(q.status))),
      delegate(panel, "click", '[data-action="clear-all"]', () => removeWhere(() => true)),
      delegate(panel, "click", '[data-action="remove"]', (e, b) => removeWhere((q) => q.id === b.dataset.qid)),
      delegate(panel, "click", '[data-action="details"]', (e, b) => openReport(queue.find((q) => q.id === b.dataset.qid))),
      delegate(panel, "click", '[data-action="draw-this"]', (e, b) => {
        const q = queue.find((x) => x.id === b.dataset.qid);
        if (!q) return;
        tabs.select("draw");
        loadDrawFromQueue(q);
      }),
      delegate(panel, "input", "[data-pwd]", (e, inp) => {
        const q = queue.find((x) => x.id === inp.dataset.pwd);
        if (q) q.password = inp.value;
      }),
      delegate(panel, "click", '[data-action="pwd-hints"]', (e, b) => openHints(queue.find((x) => x.id === b.dataset.qid))),
      delegate(panel, "click", '[data-action="apply-pwd"]', () => {
        const v = panel.querySelector("#ing-pwd-all").value;
        let n = 0;
        queue.forEach((q) => {
          if (q.isPdf && SENDABLE.has(q.status)) {
            q.password = v;
            n += 1;
            const inp = panel.querySelector(`[data-pwd="${q.id}"]`);
            if (inp) inp.value = v;
          }
        });
        toast(n ? `Password applied to ${plural(n, "PDF")}.` : "No PDFs waiting in the queue.", { timeout: 2500 });
      }),
      delegate(panel, "change", 'input[name="ing-exec-mode"]', (e, r) => {
        batchMode = r.value;
        drawModeHint();
      }),
      delegate(panel, "change", "#ing-template", (e, s) => {
        templateId = s.value;
      }),
      delegate(panel, "click", '[data-action="toggle-skip"]', (e, b) => {
        skipDuplicates = b.getAttribute("aria-checked") !== "true";
        b.setAttribute("aria-checked", skipDuplicates ? "true" : "false");
      }),
      delegate(panel, "click", '[data-action="run-agents"]', (e, b) => runAgents(b))
    );
    ctx.onCleanup(() => offs.forEach((f) => f()));

    drawQueue();
    drawTemplates();
    drawModeHint();
    drawTelemetry();
    buildHistory(panel.querySelector('[data-slot="history"]'));
  }

  // ---- queue management
  function addFiles(list) {
    const files = Array.from(list || []);
    if (!files.length) return;
    const rejected = [];
    let added = 0;
    for (const f of files) {
      const ext = extOf(f.name);
      if (!ACCEPT_EXT.has(ext)) {
        rejected.push(f.name);
        continue;
      }
      if (queue.some((q) => q.name === f.name && q.size === f.size && q.file.lastModified === f.lastModified && SENDABLE.has(q.status))) continue;
      queue.push({ id: uid("q"), file: f, name: f.name, size: f.size, ext, isPdf: ext === "pdf", password: "", hints: null, pwdCheck: null, status: "queued", error: "", report: null });
      added += 1;
    }
    if (rejected.length) toast.warn(`Unsupported file type: ${rejected.join(", ")}. Accepted: ${UPLOAD_ACCEPT}`, { title: "Some files were not added" });
    if (added) drawQueue();
  }

  function removeWhere(pred) {
    if (running) return;
    for (let i = queue.length - 1; i >= 0; i -= 1) if (pred(queue[i])) queue.splice(i, 1);
    drawQueue();
  }

  function rowSummary(q) {
    const r = q.report;
    if (q.status === "failed" || q.status === "conflict" || q.status === "duplicate") {
      return q.error ? html`<span class="small v-ingest-err">${q.error}</span>` : DASH;
    }
    if (!r) {
      if (!q.isPdf) return html`<span class="xsmall muted">${DASH}</span>`;
      return q.pwdCheck ? pwdCheckBadge(q.pwdCheck) : html`<span class="xsmall muted">PDF — set a password or holder details if it is encrypted</span>`;
    }
    const flags = [...(r.statement_flags || [])];
    const missing = r.missing_metadata || [];
    const warnings = r.warnings || [];
    const gate = gateFindings(r).filter((g) => g.tone !== "info");
    return html`<span class="small">${plural(r.transactions || 0, "row")} · ${r.bank_name || "Unknown bank"}
        ${r.account_number ? html`<span class="mono xsmall"> · ${r.account_number}</span>` : ""}</span>
      <span class="v-ingest-tags">
        ${r.password_label ? html`<span class="badge badge--info" title="Opened with: ${passwordFormula(r.password_label)}">${icon("key", { size: "sm" })}Unlocked</span>` : ""}
        ${r.date_order?.order ? html`<span class="badge badge--outline" title="${r.date_order.basis || ""}">${r.date_order.order}${r.date_order.decided === false ? "?" : ""}</span>` : ""}
        ${(r.sheets || []).length ? html`<span class="badge badge--outline">${plural(r.sheets.length, "sheet")}</span>` : ""}
        ${r.ocr_corrections ? html`<span class="badge badge--info">OCR fixes</span>` : ""}
        ${missing.length ? html`<span class="badge badge--neutral" title="${missing.join(", ")}">Missing ${missing.length}</span>` : ""}
        ${flags.filter((f) => f !== "MISSING_METADATA" && f !== "LOW_OCR_CONFIDENCE").map((f) => html`<span class="badge badge--warn">${f.replace(/_/g, " ").toLowerCase()}</span>`)}
        ${gate.map((g) => html`<span class="badge badge--${g.tone}" title="${g.detail || g.label}">${g.short || g.label}</span>`)}
        ${warnings.length ? html`<span class="badge badge--warn">${plural(warnings.length, "warning")}</span>` : ""}
      </span>`;
  }

  function queueRow(q) {
    const editable = SENDABLE.has(q.status) && !running;
    return html`<tr data-qid="${q.id}">
      <td class="v-ingest-file">
        <span class="v-ingest-ext" data-ext="${q.ext}">${q.ext.toUpperCase()}</span>
        <span class="min-w-0"><span class="strong v-ingest-name" title="${q.name}">${q.name}</span><span class="xsmall muted">${bytes(q.size)}</span></span>
      </td>
      <td>${q.isPdf
        ? html`<div class="v-ingest-pwdcell">
            <input class="input input--sm v-ingest-pwd" type="password" autocomplete="off" placeholder="Password (optional)"
              aria-label="PDF password for ${q.name}" data-pwd="${q.id}" value="${q.password}" ${editable ? "" : html`disabled`} />
            <button type="button" class="btn btn--ghost btn--sm ${hasHints(q.hints) ? "is-set" : ""}" data-action="pwd-hints" data-qid="${q.id}"
              title="Holder details to generate password candidates" aria-label="Holder details and password suggestions for ${q.name}${hasHints(q.hints) ? " (details entered)" : ""}"
              ${editable ? "" : html`disabled`}>${icon("key", { size: "sm" })}</button>
          </div>`
        : html`<span class="xsmall muted">—</span>`}</td>
      <td>${statusChip(q.status, q.status === "uploading" && q.progress ? jobLabel(q.progress) : "")}</td>
      <td class="v-ingest-summary">${rowSummary(q)}</td>
      <td class="v-ingest-rowact">
        ${q.report ? html`<button type="button" class="btn btn--ghost btn--sm" data-action="details" data-qid="${q.id}" aria-label="Details for ${q.name}">${icon("eye", { size: "sm" })}</button>` : ""}
        ${q.isPdf && !q.report ? html`<button type="button" class="btn btn--ghost btn--sm" data-action="draw-this" data-qid="${q.id}" title="Open in the PDF draw tool" aria-label="Open ${q.name} in the PDF draw tool">${icon("table", { size: "sm" })}</button>` : ""}
        ${!running ? html`<button type="button" class="btn btn--ghost btn--sm" data-action="remove" data-qid="${q.id}" aria-label="Remove ${q.name} from the queue">${icon("x", { size: "sm" })}</button>` : ""}
      </td>
    </tr>`;
  }

  function drawQueue() {
    if (!up) return;
    if (!queue.length) {
      render(up.queue, html`<p class="small muted v-ingest-empty">No files queued yet.</p>`);
    } else {
      render(
        up.queue,
        html`<div class="table-wrap"><table class="table table--compact v-ingest-table">
          <caption class="sr-only">Upload queue</caption>
          <thead><tr><th scope="col">File</th><th scope="col">Password</th><th scope="col">Status</th><th scope="col">Result</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
          <tbody>${queue.map(queueRow)}</tbody></table></div>`
      );
    }
    syncQueueActions();
    drawSources(); // keep the draw-tool PDF list in step with the queue
  }

  /** Patch one queue row in place (keeps focus on other rows' password fields). */
  function patchRow(q) {
    const tr = up?.queue.querySelector(`tr[data-qid="${q.id}"]`);
    if (!tr) return drawQueue();
    const tmp = document.createElement("tbody");
    render(tmp, queueRow(q));
    tr.replaceWith(tmp.firstElementChild);
    syncQueueActions();
    return undefined;
  }

  function syncQueueActions() {
    if (!up) return;
    const pending = queue.filter((q) => SENDABLE.has(q.status));
    const done = queue.filter((q) => ["ok", "empty", "duplicate"].includes(q.status));
    up.ingest.disabled = running || !pending.length;
    up.panel.querySelector('[data-slot="ingest-label"]').textContent = running
      ? "Ingesting…"
      : pending.length
        ? `Ingest ${plural(pending.length, "file")}`
        : "Ingest files";
    up.clearDone.hidden = running || !done.length;
    up.clearAll.hidden = running || !queue.length;
    const total = pending.reduce((a, q) => a + q.size, 0);
    up.panel.querySelector('[data-slot="queue-summary"]').textContent = queue.length
      ? `${plural(queue.length, "file")} · ${pending.length} waiting${pending.length ? ` (${bytes(total)})` : ""}`
      : "";
  }

  function drawTemplates() {
    if (!up) return;
    const tpls = (store.get().meta && store.get().meta.draw_templates) || [];
    const banks = (store.get().meta && store.get().meta.banks_supported_count) || null;
    if (templateId && !tpls.some((t) => t.template_id === templateId)) templateId = "";
    render(
      up.template,
      html`<option value="">Auto-detect${banks ? ` (${int(banks)} bank layouts)` : ""}</option>
        ${tpls.map((t) => html`<option value="${t.template_id}" ${t.template_id === templateId ? html`selected` : ""}>${t.bank_code}: ${t.template_name}</option>`)}`
    );
  }

  function drawModeHint() {
    if (!up) return;
    const cfg = store.get().llmConfig || {};
    const health = store.get().health || {};
    const provider = providerLabel(cfg.provider);
    const hasKey = Boolean(cfg.has_api_key);
    const egress = health.cloud_egress_possible ?? cfg.cloud_egress_possible;
    up.panel.querySelector('[data-slot="mode-hint"]').textContent =
      `Applies to this upload only. Global default: ${execModeLabel(cfg.execution_mode)} (change it in Settings).`;
    let warn = "";
    if (batchMode !== "heuristics" && !hasKey) {
      warn = html`<div class="callout callout--warn small">${icon("alert", { size: "sm" })}<div>No API key is saved for ${provider}, so this batch will run on <strong>heuristics only</strong>. <a href="#/settings">Configure a provider</a>.</div></div>`;
    } else if (batchMode !== "heuristics" && egress) {
      warn = html`<div class="callout callout--warn small">${icon("cloud", { size: "sm" })}<div>Cloud AI: statement content of rows the heuristics cannot handle may be sent to <strong>${provider}</strong>.</div></div>`;
    }
    render(up.panel.querySelector('[data-slot="mode-warn"]'), warn);
  }

  function providerLabel(p) {
    const cat = (store.get().meta && store.get().meta.provider_catalog) || {};
    return (cat[p] && cat[p].label) || p || "the configured provider";
  }

  // ---- ingest
  function batchesOf(items) {
    const out = [];
    let cur = [];
    let size = 0;
    for (const q of items) {
      if (cur.length && size + q.size > MAX_BATCH_BYTES) {
        out.push(cur);
        cur = [];
        size = 0;
      }
      cur.push(q);
      size += q.size;
    }
    if (cur.length) out.push(cur);
    return out;
  }

  /** Poll an ingest job until it finishes; resolves with its result, rejects with an ApiError-like error
   *  (status = the job's http_status, so a 409 still reads as a duplicate conflict). */
  async function pollJob(job, onProgress) {
    let j = job;
    let delay = 300;
    while (j.status === "queued" || j.status === "running") {
      onProgress?.(j);
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(1500, Math.round(delay * 1.25));
      j = await api.get(`/api/jobs/${encodeURIComponent(j.job_id)}`, { params: { include_result: "false" } });
    }
    if (j.status !== "succeeded") {
      throw new ApiError(j.http_status || 500, j.error || "Ingest job failed", {
        errorId: j.error_id || null, path: `/api/jobs/${j.job_id}`, body: { error: j.error, error_type: j.error_type },
      });
    }
    const full = await api.get(`/api/jobs/${encodeURIComponent(j.job_id)}`);
    return full.result || {};
  }

  async function runIngest({ forceSkip = false } = {}) {
    if (running) return;
    const items = queue.filter((q) => SENDABLE.has(q.status));
    if (!items.length) return;
    running = true;
    render(up.result, "");
    items.forEach((q) => {
      q.status = "queued";
      q.error = "";
    });
    drawQueue();
    const onDuplicate = forceSkip || skipDuplicates ? "skip" : "reject";
    const mode = batchMode;
    const all = { ingested: [], files_ok: 0, files_failed: 0, files_duplicate: 0, total_case_transactions: null, total_resolved_entities: null };
    let conflict = null;
    let anyOk = false;
    try {
      for (const batch of batchesOf(items)) {
        // P2: each file is staged in its own request (POST …/uploads), then one background ingest job is
        // started for the batch (POST …/ingest-jobs) and polled (GET /api/jobs/{id}) for progress.
        const payload = [];
        for (const q of batch) {
          q.status = "reading";
          q.progress = null;
          patchRow(q);
          try {
            const staged = await api.post(ctx.api.url("uploads"), { filename: q.name, content_b64: await fileToBase64(q.file) });
            payload.push({ upload_id: staged.upload_id, password: q.isPdf && q.password ? q.password : null, ...(q.isPdf && hasHints(q.hints) ? { password_hints: q.hints } : {}) });
            q.uploadId = staged.upload_id;
          } catch (err) {
            q.status = "failed";
            q.error = `Could not upload the file: ${err.message || err}`;
          }
          patchRow(q);
        }
        const sendQs = batch.filter((q) => q.status === "reading");
        if (!sendQs.length) continue;
        sendQs.forEach((q) => {
          q.status = "uploading";
          patchRow(q);
        });
        let res;
        try {
          // deliberately not bound to ctx.signal: the server completes the job regardless
          const job = await api.post(ctx.api.url("ingest-jobs"), {
            files: payload,
            template_id: templateId || null,
            execution_mode: mode,
            on_duplicate: onDuplicate,
          });
          res = await pollJob(job, (j) => {
            sendQs.forEach((q) => {
              q.progress = j;
              patchRow(q);
            });
          });
        } catch (err) {
          if (err && err.isConflict) {
            const names = new Set(Array.from(String(err.serverMessage || err.message).matchAll(/'([^']+)'/g), (m) => m[1]));
            sendQs.forEach((q) => {
              q.status = names.has(q.name) ? "conflict" : "queued";
              q.error = names.has(q.name) ? "Same SHA-256 as a statement already in this case (or repeated in this upload)." : "";
              q.progress = null;
            });
            conflict = err;
          } else {
            sendQs.forEach((q) => {
              q.status = "failed";
              q.error = err?.message || "Upload failed.";
              q.progress = null;
            });
            if (!isAbort(err) && ctx.isCurrent()) toast.error(err, { title: "Upload failed" });
          }
          sendQs.forEach(patchRow);
          if (conflict) break;
          continue;
        }
        sendQs.forEach((q) => {
          q.progress = null;
        });
        const reports = res.ingested || [];
        sendQs.forEach((q, i) => {
          const r = reports[i] && reports[i].filename === q.name ? reports[i] : reports.find((x) => x.filename === q.name) || reports[i];
          q.report = r || null;
          if (!r) {
            q.status = "failed";
            q.error = "No result returned for this file.";
          } else if (r.status === "ok") {
            q.status = r.transactions ? "ok" : "empty";
            q.error = "";
            // secrets are needed only for this request: forget them once the file is in the vault
            q.password = "";
            q.hints = null;
            anyOk = true;
          } else if (r.status === "duplicate") {
            q.status = "duplicate";
            q.error = r.error || "";
          } else {
            q.status = "failed";
            q.error = r.error || "Parsing failed.";
          }
          patchRow(q);
        });
        all.ingested.push(...reports);
        all.files_ok += res.files_ok || 0;
        all.files_failed += res.files_failed || 0;
        all.files_duplicate += res.files_duplicate || 0;
        all.total_case_transactions = res.total_case_transactions;
        all.total_resolved_entities = res.total_resolved_entities;
        if (res.agent_telemetry) lastTelemetry = res.agent_telemetry;
      }
    } finally {
      running = false;
      if (ctx.isCurrent()) drawQueue();
    }

    if (anyOk) {
      await appStore.mutated(MUTATION_TOPICS.upload, { caseId: ctx.caseId });
      provenance = null;
    }
    if (!ctx.isCurrent()) {
      if (all.ingested.length) toast.success(describeUploadResult(all, execModeLabel(mode)).text, { title: "Ingest finished" });
      return;
    }
    drawTelemetry();
    if (conflict) {
      const n = queue.filter((q) => q.status === "conflict").length;
      render(
        up.result,
        html`<div class="callout callout--warn" role="alert">${icon("alert")}<div class="stack-sm">
          <div><strong>${plural(n, "duplicate file")} — nothing in this batch was ingested.</strong></div>
          <div class="small">${conflict.message}</div>
          <div class="row-wrap">
            <button type="button" class="btn btn--sm btn--primary" data-action="skip-retry">Skip duplicates &amp; ingest the rest</button>
            <button type="button" class="btn btn--sm" data-action="drop-conflicts">Remove duplicates from queue</button>
          </div></div></div>`
      );
      up.panel.querySelector('[data-action="skip-retry"]')?.focus();
      if (!all.ingested.length) return;
    }
    if (all.ingested.length) {
      const d = describeUploadResult(all, execModeLabel(mode));
      const tone = d.tone === "error" ? "danger" : d.tone;
      const prev = conflict ? up.result.innerHTML : "";
      render(
        up.result,
        html`<div class="callout callout--${tone}">${icon(d.ok ? "check-circle" : "alert")}<div class="stack-sm">
          <div><strong>${d.title}</strong> · ${execModeLabel(mode)}</div>
          <div class="v-ingest-result-kpis small">
            <span><span class="strong tabular">${int(all.files_ok)}</span> ingested</span>
            <span><span class="strong tabular">${int(all.files_failed)}</span> failed</span>
            <span><span class="strong tabular">${int(all.files_duplicate)}</span> duplicate</span>
            <span><span class="strong tabular">${int(all.total_case_transactions)}</span> rows in case</span>
            ${all.total_resolved_entities != null ? html`<span><span class="strong tabular">${int(all.total_resolved_entities)}</span> entities</span>` : ""}
          </div>
          ${d.failed.length ? html`<div class="small">Failed: ${d.failed.map((f) => `${f.filename} (${f.error || "error"})`).join("; ")}</div>` : ""}
          ${anyOk ? html`<div class="row-wrap"><a class="btn btn--sm" href="${ctx.href("transactions")}">Review transactions</a>
            <button type="button" class="btn btn--sm btn--ghost" data-action="goto-statements">Custody hashes</button></div>` : ""}
        </div></div>`
      );
      if (prev) up.result.insertAdjacentHTML("afterbegin", prev); // keep the duplicate prompt visible
      if (anyOk) toast.success(`${plural(all.files_ok, "statement")} ingested.`, { title: "Ingest complete" });
    }
  }
  ctx.onCleanup(delegate(el, "click", '[data-action="goto-statements"]', () => tabs.select("statements", { focus: true })));

  function openReport(q) {
    if (!q || !q.report) return;
    const r = q.report;
    const gate = gateFindings(r);
    const a = r.anomalies || {};
    const fx = r.filename_crosscheck || null;
    const pc = r.period_coverage || null;
    const conf = r.ocr_confidence_by_page && typeof r.ocr_confidence_by_page === "object" ? Object.entries(r.ocr_confidence_by_page) : [];
    const lowPages = new Set((r.low_confidence_pages || []).map(String));
    const rejPages = new Set((r.ocr_rejected_pages || []).map(String));
    const FX_TONE = { match: "success", conflict: "danger", partial: "warn", header_only: "neutral", filename_only: "neutral", missing: "neutral", unknown: "neutral" };
    openDrawer({
      title: q.name,
      width: 560,
      className: "v-ingest", // drawers mount on <body>: reuse the view's scoped styles
      body: html`<div class="stack">
        <div class="row-wrap">${statusChip(q.status)}${r.statement_id ? html`<span class="xsmall muted mono">${r.statement_id}</span>` : ""}</div>
        ${q.error ? html`<div class="callout callout--danger small">${icon("alert", { size: "sm" })}<div>${q.error}${r.error_type ? html` <span class="mono xsmall">(${r.error_type})</span>` : ""}</div></div>` : ""}
        <dl class="kv">
          <dt>Bank</dt><dd>${r.bank_name || DASH}</dd>
          <dt>Account</dt><dd class="mono">${r.account_number || DASH}</dd>
          <dt>PAN</dt><dd class="mono">${r.pan || DASH}</dd>
          <dt>Rows</dt><dd class="tabular">${r.transactions != null ? int(r.transactions) : DASH}</dd>
          ${r.password_label ? html`<dt>Opened with</dt><dd>${passwordFormula(r.password_label)} <span class="xsmall muted">(password not stored)</span></dd>` : ""}
          <dt>File size</dt><dd>${bytes(r.file_size_bytes ?? q.size)}</dd>
          <dt>Date order</dt><dd>${r.date_order ? html`${r.date_order.order || DASH}${r.date_order.decided === false ? " (undecided)" : ""} <span class="muted">— ${r.date_order.basis || ""}</span>` : DASH}</dd>
          <dt>Sheets</dt><dd>${(r.sheets || []).length ? (r.sheets || []).map((s) => html`<div>${s.name}: ${plural(s.rows || 0, "row")}${s.header_row != null ? `, header row ${s.header_row}` : ""}</div>`) : DASH}</dd>
          <dt>OCR corrections</dt><dd>${r.ocr_corrections ? html`<pre class="code">${JSON.stringify(r.ocr_corrections, null, 2)}</pre>` : DASH}</dd>
          <dt>Missing metadata</dt><dd>${(r.missing_metadata || []).length ? (r.missing_metadata || []).join(", ") : "None"}</dd>
          <dt>Statement flags</dt><dd>${(r.statement_flags || []).length ? (r.statement_flags || []).map((f) => html`<span class="badge badge--warn">${f}</span> `) : "None"}</dd>
          <dt>SHA-256</dt><dd><span class="hash">${r.sha256_hash || DASH}</span></dd>
        </dl>
        ${r.status === "ok" ? html`<div class="v-ingest-gate"><div class="caps">Anomaly gate</div>
          ${gate.length
            ? html`<ul class="v-ingest-gate__list">${gate.map((g) => html`<li class="v-ingest-gate__item"><span class="badge badge--${g.tone}">${g.short}</span><span class="small">${g.label}</span></li>`)}</ul>`
            : html`<p class="small"><span class="badge badge--success">${icon("check-circle", { size: "sm" })}All checks passed</span>
                ${a.valid != null && a.total != null ? html` <span class="muted">${int(a.valid)} of ${int(a.total)} rows valid</span>` : ""}</p>`}</div>` : ""}
        ${fx ? html`<div><div class="caps">File name vs statement header</div>
          <div class="table-wrap"><table class="table table--compact"><caption class="sr-only">File name cross-check</caption>
            <thead><tr><th scope="col">Field</th><th scope="col">Result</th><th scope="col">Header</th><th scope="col">File name</th></tr></thead>
            <tbody>
              ${[["bank", "Bank", null, null], ["account", "Account", fx.header_account_number, fx.filename_account_number], ["holder", "Holder", fx.header_holder_name, fx.filename_holder_name], ["period", "Period", null, fx.filename_period]]
                .filter(([k]) => fx[k] != null)
                .map(([k, label, hv, fv]) => html`<tr><td>${label}</td><td><span class="badge badge--${FX_TONE[fx[k]] || "neutral"}">${String(fx[k]).replace(/_/g, " ")}</span></td>
                  <td class="mono xsmall">${hv || DASH}</td><td class="mono xsmall">${fv == null ? DASH : typeof fv === "object" ? JSON.stringify(fv) : fv}</td></tr>`)}
            </tbody></table></div></div>` : ""}
        ${pc ? html`<div><div class="caps">Period coverage</div>
          <dl class="kv">
            <dt>Stated period</dt><dd>${pc.stated_start ? date(pc.stated_start) : DASH} → ${pc.stated_end ? date(pc.stated_end) : DASH}</dd>
            <dt>Rows span</dt><dd>${pc.first_txn_date ? date(pc.first_txn_date) : DASH} → ${pc.last_txn_date ? date(pc.last_txn_date) : DASH}</dd>
            <dt>Rows outside</dt><dd class="tabular">${pc.rows_outside != null ? int(pc.rows_outside) : DASH}</dd>
            <dt>Days without rows</dt><dd>${pc.days_without_rows_at_start != null ? `${int(pc.days_without_rows_at_start)} at start` : DASH} · ${pc.days_without_rows_at_end != null ? `${int(pc.days_without_rows_at_end)} at end` : DASH}</dd>
          </dl></div>` : ""}
        ${conf.length ? html`<div><div class="caps">OCR confidence by page</div>
          <div class="table-wrap"><table class="table table--compact"><caption class="sr-only">OCR confidence per page</caption>
            <thead><tr><th scope="col">Page</th><th scope="col" class="num">Confidence</th><th scope="col">Status</th></tr></thead>
            <tbody>${conf.sort((x, y) => Number(x[0]) - Number(y[0])).map(([pg, v]) => html`<tr><td>${pg}</td><td class="num">${v == null ? DASH : `${(Number(v) * 100).toFixed(0)}%`}</td>
              <td>${rejPages.has(String(pg)) ? html`<span class="badge badge--danger">Rejected</span>` : lowPages.has(String(pg)) ? html`<span class="badge badge--warn">Low</span>` : html`<span class="badge badge--success">OK</span>`}</td></tr>`)}</tbody>
          </table></div></div>` : ""}
        ${(r.warnings || []).length ? html`<div><div class="caps">Warnings</div><ul class="v-ingest-list">${r.warnings.map((w) => html`<li>${w}</li>`)}</ul></div>` : ""}
        ${(r.deterministic_warnings || []).length ? html`<div><div class="caps">Parser warnings (before AI repair)</div><ul class="v-ingest-list">${r.deterministic_warnings.map((w) => html`<li>${w}</li>`)}</ul></div>` : ""}
      </div>`,
    });
  }

  // ---- holder details -> password candidates (PDF-13). Nothing here touches storage: values stay on the queue
  // item (memory of this tab) and are dropped after a successful ingest.
  function describeCheck(c) {
    if (!c) return "";
    if (c.status === "ok") {
      return html`<div class="callout callout--success small" role="status">${icon("check-circle", { size: "sm" })}<div>
        <strong>Unlocked</strong> with ${passwordFormula(c.label)} (candidate ${int(c.tried)} of ${int(c.candidates)}).
        Ingest will use the same details; the password itself is never shown, saved or logged.</div></div>`;
    }
    if (c.status === "no_match") {
      return html`<div class="callout callout--warn small" role="status">${icon("alert", { size: "sm" })}<div>
        <strong>None of the ${plural(c.candidates, "candidate")} opened this PDF.</strong> Check the date of birth and name spelling,
        add the customer ID or mobile, or list known passwords one per line.</div></div>`;
    }
    if (c.status === "not_encrypted") {
      return html`<div class="callout callout--info small" role="status">${icon("info", { size: "sm" })}<div>This PDF is not password-protected; no password is needed.</div></div>`;
    }
    return html`<div class="callout callout--warn small" role="status">${icon("alert", { size: "sm" })}<div>
      The server has no PDF decryption engine (install pikepdf or Poppler), so candidates cannot be tested here. The details will still be sent with the upload.</div></div>`;
  }

  function openHints(q) {
    if (!q || !q.isPdf || running) return;
    const guess = hasHints(q.hints) ? {} : hintsFromFilename(q.name);
    const cur = { ...guess, ...(q.hints || {}) };
    const otherPdfs = queue.filter((x) => x !== q && x.isPdf && SENDABLE.has(x.status)).length;
    const body = html`<div class="stack">
      <p class="small text-2">The server generates the usual bank formulas from these details (DOB as DDMMYYYY, PAN, first four letters of the name + DOB, …),
        tries them in memory and reports <strong>only which formula worked</strong>. Nothing here is saved to the case, the audit log or this browser.</p>
      ${Object.keys(guess).length ? html`<p class="xsmall muted row">${icon("info", { size: "sm" })}<span>Pre-filled from the file name — check before testing.</span></p>` : ""}
      <div class="form-grid v-ingest-hints">
        ${HINT_FIELDS.map((f) => {
          const id = `ing-h-${f.key}`;
          const v = f.multi ? (cur[f.key] || []).join("\n") : cur[f.key] || "";
          return f.multi
            ? html`<div class="field v-ingest-hints__wide"><label class="field__label" for="${id}">${f.label}</label>
                <textarea class="input mono" id="${id}" name="${f.key}" rows="3" autocomplete="off" spellcheck="false" placeholder="${f.ph}">${v}</textarea></div>`
            : html`<div class="field"><label class="field__label" for="${id}">${f.label}</label>
                <input class="input ${f.key === "pan" ? "mono" : ""}" id="${id}" name="${f.key}" type="${f.type || "text"}" value="${v}" autocomplete="off" spellcheck="false"
                  ${f.ph ? html`placeholder="${f.ph}"` : ""} ${f.max ? html`maxlength="${f.max}"` : ""} ${f.mode ? html`inputmode="${f.mode}"` : ""} /></div>`;
        })}
      </div>
      ${otherPdfs ? html`<label class="row small"><input type="checkbox" name="apply_all" /> Use these details for the other ${plural(otherPdfs, "PDF")} in the queue too</label>` : ""}
      <div data-slot="pwd-result" aria-live="polite">${describeCheck(q.pwdCheck)}</div>
    </div>`;
    const save = (values) => {
      const h = cleanHints(values);
      const targets = values.apply_all ? queue.filter((x) => x.isPdf && SENDABLE.has(x.status)) : [q];
      targets.forEach((x) => {
        x.hints = hasHints(h) ? { ...h } : null;
        if (x !== q) x.pwdCheck = null;
        patchRow(x);
      });
      return h;
    };
    const m = openModal({
      title: "Suggest a PDF password",
      description: q.name,
      size: "md",
      className: "v-ingest-hintsdlg",
      body,
      actions: [
        { label: "Clear details", value: "clear" },
        { spacer: true },
        { label: "Done", value: "done" },
        { label: "Test candidates", variant: "primary", type: "submit" },
      ],
      onSubmit: async (values, { el: dlg }) => {
        const h = save(values);
        const res = await api.post(
          "/api/pdf/password-check",
          { content_b64: await fileToBase64(q.file), password: q.password || null, password_hints: hasHints(h) ? h : null },
          { signal: ctx.signal }
        );
        if (!ctx.isCurrent()) return undefined;
        q.pwdCheck = res;
        patchRow(q);
        const slot = dlg.querySelector('[data-slot="pwd-result"]');
        if (slot) {
          render(slot, describeCheck(res));
          slot.scrollIntoView({ block: "nearest" }); // the outcome sits below the fields on short screens
        }
        return false; // keep the dialog open to show the outcome
      },
    });
    m.closed.then((v) => {
      if (!ctx.isCurrent()) return;
      if (v === "clear") {
        q.hints = null;
        q.pwdCheck = null;
        patchRow(q);
      } else if (v === "done") {
        save(formValues(m.form));
      }
    });
    ctx.onCleanup(() => m.close());
  }

  // ---- telemetry + run agents
  function drawTelemetry() {
    const slot = up?.panel.querySelector('[data-slot="telemetry"]');
    if (!slot) return;
    const t = describeAgentTelemetry(lastTelemetry);
    if (!t) {
      render(slot, html`<p class="small muted">No pipeline run recorded for this case yet. Runs appear here after an upload.</p>`);
      return;
    }
    const invoked = new Set((t.stagesInvoked || []).map(Number));
    const sd = lastTelemetry.stage_details || {};
    render(
      slot,
      html`<div class="stack-sm">
        <div class="small"><span class="badge badge--outline">${execModeLabel(t.mode)}</span> ${t.summary}
          ${lastTelemetry.timestamp ? html`<span class="xsmall muted"> · ${dateTime(lastTelemetry.timestamp)}</span>` : ""}</div>
        <ol class="v-ingest-stages" aria-label="Agent stages">
          ${[1, 2, 3, 4].map((n) => {
            const on = invoked.has(n) || sd[`stage${n}`]?.invoked;
            return html`<li class="v-ingest-stage ${on ? "is-on" : ""}"><span class="strong">S${n}</span>
              <span class="xsmall">${on ? "Invoked" : "Not needed"}</span></li>`;
          })}
        </ol>
        ${t.warnings.length ? html`<ul class="v-ingest-list small">${t.warnings.map((w) => html`<li>${w}</li>`)}</ul>` : ""}
      </div>`
    );
  }

  async function runAgents(btn) {
    const cfg = store.get().llmConfig || {};
    if (!cfg.has_api_key) {
      ctx.toast.warn("The AI backup agents need an API key. Configure a provider in Settings first.", {
        title: "No AI provider",
        action: { label: "Open settings", run: () => (window.location.hash = "#/settings") },
      });
      return;
    }
    const mode = batchMode === "heuristics" ? "both" : batchMode;
    const ok = await confirmDialog({
      title: "Run AI backup agents?",
      message: `Stages 2–4 re-run over this case in ${execModeLabel(mode)} mode. Narrations and statement metadata will be sent to ${providerLabel(cfg.provider)}.`,
      confirmLabel: "Run agents",
    });
    if (!ok || !ctx.isCurrent()) return;
    const res = await withBusy(btn, () => ctx.api.post("run-agents", { execution_mode: mode }), { error: "Agent run failed" });
    if (!res) return;
    lastTelemetry = res.agent_telemetry || lastTelemetry;
    await ctx.mutated(MUTATION_TOPICS.runAgents);
    if (!ctx.isCurrent()) return;
    drawTelemetry();
    const stages = (res.agent_telemetry?.stages_invoked || []).map((s) => `S${s}`).join(", ");
    toast.success(`${plural(res.updated_transactions || 0, "row")} re-processed. Invoked: ${stages || "none (heuristics sufficed)"}.`, { title: "Agents finished" });
  }

  // ---- history
  let historyGrid = null;
  function buildHistory(slot) {
    historyGrid = createDataGrid(slot, {
      ariaLabel: "Ingest history",
      rows: (store.get().caseData && store.get().caseData.recent_ingest_results) || [],
      height: "260px",
      rowHeight: 40,
      showToolbar: false,
      countNoun: ["file result", "file results"],
      initialSort: { key: "created_at", order: "desc" },
      emptyState: { icon: "inbox", title: "No uploads yet", message: "Per-file outcomes appear here after the first upload.", compact: true },
      columns: [
        { key: "created_at", label: "When", width: "150px", sortable: true, format: (v) => dateTime(v), title: (r) => r.created_at },
        { key: "filename", label: "File", width: "minmax(200px, 2fr)", sortable: true, render: (r) => html`<span class="truncate" title="${r.filename}">${r.filename}</span>` },
        { key: "status", label: "Status", width: "150px", sortable: true, render: (r) => statusChip(r.status === "ok" && !r.transactions ? "empty" : r.status) },
        { key: "transactions", label: "Rows", width: "80px", align: "right", sortable: true, format: (v) => (v == null ? DASH : int(v)) },
        { key: "file_size_bytes", label: "Size", width: "90px", align: "right", sortable: true, format: (v) => bytes(v) },
        { key: "error", label: "Error", width: "minmax(200px, 2fr)", title: (r) => r.error || "", render: (r) => (r.error ? html`<span class="small v-ingest-err">${r.error}</span>` : DASH) },
      ],
    });
    ctx.onCleanup(() => historyGrid.destroy());
  }

  // ===================================================================================== STATEMENTS
  let stmtGrid = null;
  function buildStatements(panel) {
    render(
      panel,
      html`<div class="stack">
        <div data-slot="stmt-summary"></div>
        <section class="card" aria-labelledby="ing-st-title">
          <div class="card__head"><div><h2 class="card__title" id="ing-st-title">Statements &amp; chain of custody</h2>
            <p class="card__sub">Full SHA-256 of each source file as received. Select a statement for its provenance record.</p></div></div>
          <div class="card__body" data-slot="stmt-grid"></div>
        </section>
      </div>`
    );
    stmtGrid = createDataGrid(panel.querySelector('[data-slot="stmt-grid"]'), {
      ariaLabel: "Ingested statements",
      rowKey: "statement_id",
      rows: store.statements(),
      height: "min(64vh, 620px)",
      rowHeight: 58,
      countNoun: ["statement", "statements"],
      initialSort: { key: "period_start", order: "asc" },
      filters: [{ key: "q", type: "search", label: "Search", placeholder: "File, bank, holder, account…" }],
      clientFilter: (s, f) => {
        const q = String(f.q || "").trim().toLowerCase();
        return !q || [s.source_filename, s.bank_name, s.account_holder_name, s.account_number, s.ifsc_code].some((v) => String(v || "").toLowerCase().includes(q));
      },
      onRowClick: (s) => openProvenance(s),
      emptyState: { icon: "upload", title: "No statements yet", message: "Upload PDF, Excel, CSV or image statements on the Upload tab.", compact: true },
      columns: [
        { key: "source_filename", label: "File & period", width: "minmax(220px, 2fr)", sortable: true,
          render: (s) => html`<span class="strong truncate" title="${s.source_filename}">${s.source_filename}</span><span class="dg__sub">${date(s.period_start)} → ${date(s.period_end)}</span>` },
        { key: "bank_name", label: "Bank & account", width: "minmax(180px, 1.4fr)", sortable: true,
          render: (s) => html`${s.bank_name || DASH}<span class="dg__sub mono">${s.account_number || ""}${s.ifsc_code ? ` · ${s.ifsc_code}` : ""}</span>` },
        { key: "account_holder_name", label: "Holder", width: "minmax(150px, 1.1fr)", sortable: true,
          render: (s) => html`${s.account_holder_name || DASH}<span class="dg__sub">${s.account_type || ""}</span>` },
        { key: "total_transactions", label: "Rows", width: "72px", align: "right", sortable: true, format: (v) => int(v) },
        { key: "opening_balance", label: "Opening", width: "120px", align: "right", sortable: true, format: (v) => inr(v) },
        { key: "closing_balance", label: "Closing", width: "120px", align: "right", sortable: true, format: (v) => inr(v) },
        { key: "balance_mismatch_count", label: "Balance check", width: "150px", sortable: true,
          render: (s) => html`${s.balance_mismatch_count
            ? html`<span class="badge badge--danger">${icon("x-circle", { size: "sm" })}${int(s.balance_mismatch_count)} mismatch</span>`
            : html`<span class="badge badge--success">${icon("check-circle", { size: "sm" })}Balanced</span>`}
            <span class="dg__sub">${s.duplicate_suspect_count ? html`<span class="badge badge--warn">${int(s.duplicate_suspect_count)} dup</span> ` : ""}${s.is_ocr_derived ? html`<span class="badge badge--info">OCR</span>` : ""}</span>` },
        { key: "sha256_hash", label: "SHA-256 (custody)", width: "minmax(300px, 2fr)", wrap: true,
          title: (s) => s.sha256_hash, render: (s) => html`<span class="hash">${s.sha256_hash}</span>` },
      ],
    });
    ctx.onCleanup(() => stmtGrid.destroy());
    drawStmtSummary();
  }

  function drawStmtSummary() {
    const slot = el.querySelector('[data-slot="stmt-summary"]');
    if (!slot) return;
    const st = store.statements();
    const bad = st.filter((s) => s.balance_mismatch_count > 0);
    const mismatches = bad.reduce((a, s) => a + s.balance_mismatch_count, 0);
    const ocr = st.filter((s) => s.is_ocr_derived).length;
    render(
      slot,
      st.length
        ? html`<div class="callout ${bad.length ? "callout--danger" : "callout--success"} small">${icon(bad.length ? "alert" : "check-circle", { size: "sm" })}
            <div>${bad.length
              ? html`<strong>${plural(bad.length, "statement")}</strong> of ${int(st.length)} fail the running-balance check (${plural(mismatches, "mismatch", "mismatches")}). <a href="${ctx.href("anomalies")}">Review anomalies</a>.`
              : html`All ${plural(st.length, "statement")} reconcile to their running balances.`}
              ${ocr ? html` ${plural(ocr, "statement")} derived from OCR.` : ""}</div></div>`
        : ""
    );
  }

  function loadProvenance() {
    if (provenance) return Promise.resolve(provenance);
    if (!provenancePromise) {
      provenancePromise = ctx.api
        .get("provenance")
        .then((p) => {
          provenance = p;
          return p;
        })
        .finally(() => {
          provenancePromise = null;
        });
    }
    return provenancePromise;
  }

  function openProvenance(s) {
    const d = openDrawer({
      title: s.source_filename,
      width: 600,
      className: "v-ingest",
      body: html`<div class="stack">
        <div class="row-wrap">
          ${s.balance_mismatch_count ? html`<span class="badge badge--danger">${int(s.balance_mismatch_count)} balance mismatch</span>` : html`<span class="badge badge--success">Balanced</span>`}
          ${s.duplicate_suspect_count ? html`<span class="badge badge--warn">${int(s.duplicate_suspect_count)} duplicate suspects</span>` : ""}
          ${s.is_ocr_derived ? html`<span class="badge badge--info">OCR-derived</span>` : ""}
        </div>
        <dl class="kv">
          <dt>Bank</dt><dd>${s.bank_name || DASH} ${s.bank_code ? html`<span class="muted mono xsmall">${s.bank_code}</span>` : ""}</dd>
          <dt>Account</dt><dd class="mono">${s.account_number || DASH}</dd>
          <dt>IFSC</dt><dd class="mono">${s.ifsc_code || DASH}</dd>
          <dt>Holder</dt><dd>${s.account_holder_name || DASH}${s.account_type ? ` · ${s.account_type}` : ""}</dd>
          <dt>Period</dt><dd>${date(s.period_start)} → ${date(s.period_end)}</dd>
          <dt>Opening / closing</dt><dd class="tabular">${inr(s.opening_balance)} → ${inr(s.closing_balance)}</dd>
          <dt>Rows</dt><dd class="tabular">${int(s.total_transactions)}</dd>
          <dt>Ingested</dt><dd>${dateTime(s.ingested_at)}</dd>
          <dt>Statement ID</dt><dd class="mono xsmall">${s.statement_id}</dd>
        </dl>
        <div class="stack-sm">
          <div class="row-between"><span class="caps">SHA-256</span>
            <button type="button" class="btn btn--ghost btn--sm" data-action="copy-hash">${icon("link", { size: "sm" })}Copy</button></div>
          <span class="hash">${s.sha256_hash}</span>
        </div>
        <div data-slot="prov">${spinner("Loading provenance")}</div>
      </div>`,
      footer: s.balance_mismatch_count ? html`<a class="btn btn--sm" href="${ctx.href("anomalies")}">Review anomalies${icon("arrow-right", { size: "sm" })}</a>` : null,
    });
    d.body.querySelector('[data-action="copy-hash"]').addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(s.sha256_hash);
        toast.success("SHA-256 copied.", { timeout: 1800 });
      } catch (_e) {
        toast.warn("Clipboard unavailable — select the hash and copy it manually.");
      }
    });
    loadProvenance()
      .then((p) => {
        const slot = d.body.querySelector('[data-slot="prov"]');
        if (!slot || !slot.isConnected) return;
        const recs = (p.provenance || []).filter((r) => r.statement_id === s.statement_id);
        const runs = (p.ingest_results || []).filter((r) => r.statement_id === s.statement_id || r.sha256 === s.sha256_hash);
        render(
          slot,
          html`<div class="stack">
            <div><div class="caps">Provenance</div>
              ${recs.length
                ? recs.map(
                    (r) => html`<dl class="kv v-ingest-prov">
                      <dt>Parser</dt><dd class="mono xsmall">${r.parser_version || DASH}</dd>
                      <dt>Analyst</dt><dd>${r.analyst || DASH}</dd>
                      <dt>Recorded</dt><dd>${dateTime(r.ingested_at)}</dd>
                      <dt>File size</dt><dd>${r.file_size_bytes != null ? bytes(r.file_size_bytes) : DASH}</dd>
                      <dt>Hash match</dt><dd>${r.sha256 === s.sha256_hash
                        ? html`<span class="badge badge--success">${icon("check", { size: "sm" })}Matches statement</span>`
                        : html`<span class="badge badge--danger">Differs</span> <span class="hash">${r.sha256}</span>`}</dd>
                      <dt>Details</dt><dd>${r.details && Object.keys(r.details).length ? html`<pre class="code">${JSON.stringify(r.details, null, 2)}</pre>` : DASH}</dd>
                    </dl>`
                  )
                : html`<p class="small muted">No provenance record for this statement.</p>`}
            </div>
            <div><div class="caps">Ingest attempts</div>
              ${runs.length
                ? html`<div class="table-wrap"><table class="table table--compact"><thead><tr><th scope="col">When</th><th scope="col">Status</th><th scope="col" class="num">Rows</th><th scope="col">Batch</th></tr></thead>
                    <tbody>${runs.map((r) => html`<tr><td>${dateTime(r.created_at)}</td><td>${statusChip(r.status)}</td><td class="num">${r.transactions != null ? int(r.transactions) : DASH}</td><td class="mono xsmall">${r.batch_id || DASH}</td></tr>`)}</tbody></table></div>`
                : html`<p class="small muted">No per-file ingest results recorded (statement predates ingest tracking).</p>`}
            </div></div>`
        );
      })
      .catch((err) => {
        if (isAbort(err)) return;
        const slot = d.body.querySelector('[data-slot="prov"]');
        if (slot) render(slot, errorState(err, { title: "Could not load provenance", retryId: "", compact: true }));
      });
  }

  // ===================================================================================== DRAW TOOL
  const draw = {
    preview: null,
    page: 1,
    bbox: [...DRAW_DEFAULTS.bbox],
    splits: [...DRAW_DEFAULTS.splits],
    names: [...DRAW_DEFAULTS.names],
    file: null,
    fileName: "",
    b64: null, // base64 of `file`, cached for the page-image calls (memory only)
    b64File: null,
    images: new Map(), // page -> data URL of the server raster (PDF-21)
    rasterState: "", // "" | loading | ok | unavailable | locked | error
    showTokens: null, // null = automatic (text layer shown only when no raster)
    hints: null, // holder-detail password hints of a queued PDF (memory only; the server resolves the password)
  };
  const SVG_NS = "http://www.w3.org/2000/svg";
  function setDrawFile(file, name) {
    if (draw.file !== file) {
      draw.images = new Map();
      draw.b64 = null;
      draw.b64File = null;
      draw.rasterState = "";
    }
    if (draw.file !== file) draw.hints = null;
    draw.file = file;
    draw.fileName = name;
  }
  async function drawB64() {
    if (draw.b64 && draw.b64File === draw.file) return draw.b64;
    const f = draw.file;
    const b = await fileToBase64(f);
    if (draw.file === f) {
      draw.b64 = b;
      draw.b64File = f;
    }
    return b;
  }
  let dv = null; // draw view refs
  let drawRaf = 0;
  ctx.onCleanup(() => cancelAnimationFrame(drawRaf));

  function buildDraw(panel) {
    const fileId = uid("draw-file");
    render(
      panel,
      html`<div class="stack">
        <section class="card" aria-labelledby="ing-dr-title">
          <div class="card__head"><div><h2 class="card__title" id="ing-dr-title">Calibrate a PDF layout</h2>
            <p class="card__sub">For bank layouts auto-detect misreads: frame the transaction table, place the column dividers, name each column and save it as a template.</p></div></div>
          <div class="card__body stack">
            <div class="v-ingest-drawsrc">
              <div class="field">
                <label class="field__label" for="ing-dr-src">PDF</label>
                <select class="select" id="ing-dr-src" data-slot="draw-src"></select>
                <input type="file" id="${fileId}" class="sr-only" accept=".pdf" tabindex="-1" aria-hidden="true" data-draw-file />
              </div>
              <div class="field">
                <label class="field__label" for="ing-dr-pwd">Password</label>
                <input class="input" type="password" id="ing-dr-pwd" autocomplete="off" placeholder="If encrypted" />
              </div>
              <div class="row v-ingest-drawsrc__btns">
                <button type="button" class="btn" data-action="draw-pick">${icon("folder", { size: "sm" })}Choose PDF…</button>
                <button type="button" class="btn btn--primary" data-action="draw-load">${icon("eye", { size: "sm" })}Load preview</button>
              </div>
            </div>
            <div data-slot="draw-msg" aria-live="polite"></div>
          </div>
        </section>
        <div data-slot="draw-work"></div>
      </div>`
    );
    dv = { panel, src: panel.querySelector('[data-slot="draw-src"]'), fileInput: panel.querySelector("[data-draw-file]"), msg: panel.querySelector('[data-slot="draw-msg"]'), work: panel.querySelector('[data-slot="draw-work"]') };
    drawSources();
    drawWorkEmpty();
    const offs = [
      delegate(panel, "click", '[data-action="draw-pick"]', () => dv.fileInput.click()),
      delegate(panel, "change", "[data-draw-file]", () => {
        const f = dv.fileInput.files && dv.fileInput.files[0];
        dv.fileInput.value = "";
        if (!f) return;
        if (extOf(f.name) !== "pdf") {
          toast.warn("The draw tool works on PDF statements only.");
          return;
        }
        setDrawFile(f, f.name);
        drawSources();
        loadPreview(panel.querySelector('[data-action="draw-load"]'));
      }),
      delegate(panel, "change", "#ing-dr-src", (e, s) => {
        const q = queue.find((x) => x.id === s.value);
        if (q) {
          setDrawFile(q.file, q.name);
          panel.querySelector("#ing-dr-pwd").value = q.password || "";
        }
      }),
      delegate(panel, "click", '[data-action="draw-load"]', (e, b) => loadPreview(b)),
    ];
    ctx.onCleanup(() => offs.forEach((f) => f()));
  }

  function drawSources() {
    if (!dv) return;
    const pdfs = queue.filter((q) => q.isPdf);
    const extra = draw.file && !pdfs.some((q) => q.file === draw.file) ? [{ id: "__picked", name: draw.fileName }] : [];
    const opts = [...extra, ...pdfs];
    if (!draw.file && pdfs.length) setDrawFile(pdfs[0].file, pdfs[0].name);
    render(
      dv.src,
      opts.length
        ? html`${opts.map((q) => html`<option value="${q.id}" ${(q.file || draw.file) === draw.file && q.name === draw.fileName ? html`selected` : ""}>${q.name}</option>`)}`
        : html`<option value="">No PDF selected — choose one</option>`
    );
  }

  function loadDrawFromQueue(q) {
    setDrawFile(q.file, q.name);
    // A PDF unlocked through holder details: the server resolves the password from the same hints (never sent back)
    draw.hints = hasHints(q.hints) ? { ...q.hints } : null;
    drawSources();
    if (dv) dv.panel.querySelector("#ing-dr-pwd").value = q.password || "";
    loadPreview(dv?.panel.querySelector('[data-action="draw-load"]'));
  }

  function drawWorkEmpty() {
    render(
      dv.work,
      html`<div class="card">${emptyState({
        icon: "table",
        title: "No PDF loaded",
        message: "Choose a PDF statement (or one from the upload queue) and load its first-page preview.",
        compact: true,
      })}</div>`
    );
  }

  async function loadPreview(btn) {
    if (!draw.file) {
      toast.warn("Choose a PDF statement first.");
      return;
    }
    const pwd = dv.panel.querySelector("#ing-dr-pwd").value || null;
    const res = await withBusy(
      btn,
      async () => ctx.api.post("pdf-preview", { content_b64: await drawB64(), password: pwd, ...(draw.hints ? { password_hints: draw.hints } : {}) }),
      { error: "Preview failed" }
    );
    if (!res || !ctx.isCurrent()) return;
    if (res.is_encrypted) {
      // Never leave the previous file's page, frame and parsed rows under the lock message (they belong to
      // another PDF and could be saved as this one's template).
      draw.preview = null;
      draw.images = new Map();
      draw.rasterState = "";
      drawWorkEmpty();
      render(dv.msg, html`<div class="callout callout--warn" role="alert">${icon("lock")}<div>This PDF is password-protected${res.error ? ` (${res.error})` : ""}. Enter the password and load it again.</div></div>`);
      dv.panel.querySelector("#ing-dr-pwd").focus();
      return;
    }
    const notes = [];
    if (res.error) notes.push(html`<div class="callout callout--warn small">${icon("alert", { size: "sm" })}<div>${res.error}</div></div>`);
    if (res.needs_ocr) notes.push(html`<div class="callout callout--info small">${icon("info", { size: "sm" })}<div>No usable text layer${(res.ocr_pages || []).length ? ` on page(s) ${res.ocr_pages.join(", ")}` : ""}; tokens come from OCR (${res.text_source || "OCR"}) and may be sparse.</div></div>`);
    render(dv.msg, html`${notes}`);
    draw.preview = res;
    draw.page = 1;
    draw.images = new Map();
    draw.rasterState = "";
    buildWorkspace();
    loadRaster();
  }

  // -- page raster (POST /api/draw/page-image): drawn under the overlay in PDF points, so the frame and the
  //    dividers line up with the tokens the parser sees. Falls back to the text layer when unavailable.
  let rasterSeq = 0;
  async function loadRaster() {
    const page = draw.page;
    const file = draw.file;
    if (!file) return;
    if (draw.images.has(page)) {
      draw.rasterState = "ok";
      paintRaster();
      return;
    }
    const seq = ++rasterSeq;
    draw.rasterState = "loading";
    paintRaster();
    const pwd = dv.panel.querySelector("#ing-dr-pwd").value || null;
    try {
      const res = await api.post("/api/draw/page-image", { content_b64: await drawB64(), page, dpi: DRAW_DPI, password: pwd, ...(draw.hints ? { password_hints: draw.hints } : {}) }, { signal: ctx.signal });
      if (!ctx.isCurrent() || seq !== rasterSeq || draw.file !== file) return;
      const b64 = String(res.png_b64 || "");
      if (!/^[A-Za-z0-9+/=]+$/.test(b64)) throw new Error("The page image is not a PNG.");
      draw.images.set(page, `data:image/png;base64,${b64}`);
      draw.rasterState = "ok";
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || seq !== rasterSeq) return;
      const code = err?.body?.code || "";
      draw.rasterState = code === "renderer_unavailable" ? "unavailable" : code === "password_required" || code === "wrong_password" ? "locked" : "error";
      draw.rasterError = err?.message || "";
    }
    if (draw.page === page) paintRaster();
  }

  function paintRaster() {
    const g = dv?.work.querySelector("[data-raster]");
    const msg = dv?.work.querySelector('[data-slot="raster-msg"]');
    if (!g) return;
    const { pw, ph } = pageSize();
    const url = draw.rasterState === "ok" ? draw.images.get(draw.page) : null;
    g.replaceChildren();
    if (url) {
      const img = document.createElementNS(SVG_NS, "image");
      img.setAttribute("class", "v-ingest-raster");
      img.setAttribute("x", "0");
      img.setAttribute("y", "0");
      img.setAttribute("width", pw.toFixed(1));
      img.setAttribute("height", ph.toFixed(1));
      img.setAttribute("preserveAspectRatio", "none");
      img.setAttribute("href", url);
      g.appendChild(img);
    }
    syncTokenLayer();
    if (!msg) return;
    const notes = {
      loading: html`${spinner("Rendering page image")}<span class="xsmall muted">Rendering page image…</span>`,
      unavailable: html`<span class="xsmall muted row">${icon("info", { size: "sm" })} No PDF renderer on the server (renderer_unavailable): showing the text layer only.</span>`,
      locked: html`<span class="xsmall v-ingest-err row">${icon("lock", { size: "sm" })} The page image needs the PDF password: enter it above and load the preview again.</span>`,
      error: html`<span class="xsmall v-ingest-err row">${icon("alert", { size: "sm" })} Page image failed${draw.rasterError ? `: ${draw.rasterError}` : ""}. Showing the text layer.</span>`,
    };
    render(msg, notes[draw.rasterState] || "");
  }

  function syncTokenLayer() {
    const toks = dv?.work.querySelector("[data-tokens]");
    const box = dv?.work.querySelector('[data-draw="tokens"]');
    const hasImg = draw.rasterState === "ok" && draw.images.has(draw.page);
    const show = draw.showTokens == null ? !hasImg : draw.showTokens;
    if (toks) toks.classList.toggle("is-hidden", !show);
    if (toks) toks.classList.toggle("is-over-image", hasImg);
    if (box) {
      box.checked = show;
      box.disabled = !hasImg;
    }
  }

  const pageSize = () => {
    const ps = (draw.preview && draw.preview.page_sizes) || [];
    const p = ps[draw.page - 1] || ps[0] || [595, 842];
    return { pw: Number(p[0]) || 595, ph: Number(p[1]) || 842 };
  };
  const pageTokens = () => ((draw.preview && draw.preview.tokens) || []).filter((t) => Number(t.page) === draw.page);

  function buildWorkspace() {
    const pages = ((draw.preview && draw.preview.page_sizes) || []).length || 1;
    const { pw, ph } = pageSize();
    const toks = pageTokens();
    render(
      dv.work,
      html`<div class="v-ingest-drawgrid">
        <section class="card min-w-0" aria-labelledby="ing-dr-page">
          <div class="card__head">
            <div><h2 class="card__title" id="ing-dr-page">${draw.fileName}</h2>
              <p class="card__sub">Drag across the page to draw the table frame. Drag its edges and the dividers, or focus one and use the arrow keys (Shift = larger steps, Delete removes a divider).</p></div>
            <div class="row-wrap">
              <label class="row xsmall"><input type="checkbox" data-draw="tokens" /> Text layer</label>
              ${pages > 1
                ? html`<label class="sr-only" for="ing-dr-pg">Page</label>
                  <select class="select select--sm input--w-sm" id="ing-dr-pg" data-draw="page">
                    ${Array.from({ length: pages }, (_, i) => html`<option value="${i + 1}" ${i + 1 === draw.page ? html`selected` : ""}>Page ${i + 1} of ${pages}</option>`)}
                  </select>`
                : html`<span class="xsmall muted">1 page</span>`}
            </div>
          </div>
          <div class="card__body v-ingest-pagewrap">
            <svg class="v-ingest-page" viewBox="0 0 ${pw.toFixed(1)} ${ph.toFixed(1)}" role="group" data-svg
              aria-label="Page ${draw.page} of ${draw.fileName}: drag on the page to draw the table frame, then adjust its edges and the column dividers" aria-describedby="ing-dr-rows-desc">
              <rect class="v-ingest-paper" x="0" y="0" width="${pw.toFixed(1)}" height="${ph.toFixed(1)}"></rect>
              <g data-raster aria-hidden="true"></g>
              <g class="v-ingest-tokens" aria-hidden="true" data-tokens>
                ${toks.map((t) => html`<text x="${Number(t.x).toFixed(1)}" y="${(ph - Number(t.y)).toFixed(1)}">${t.text}</text>`)}
              </g>
              <g data-overlay></g>
            </svg>
            <div class="row-wrap" data-slot="raster-msg" aria-live="polite"></div>
            <p class="xsmall muted" id="ing-dr-rows-desc">${plural(toks.length, "text token")} on this page${(draw.preview.tokens || []).length >= 400 ? " (the preview carries the first 400 tokens of the document)" : ""}. The parsed-row preview below lists what the parser will read inside the frame.</p>
          </div>
        </section>
        <div class="stack min-w-0">
          <section class="card" aria-labelledby="ing-dr-cols">
            <div class="card__head"><h2 class="card__title" id="ing-dr-cols">Columns</h2>
              <div class="btn-group">
                <button type="button" class="btn btn--sm" data-draw="add">${icon("plus", { size: "sm" })}Divider</button>
                <button type="button" class="btn btn--sm" data-draw="remove" aria-label="Remove last divider">${icon("minus", { size: "sm" })}</button>
              </div></div>
            <div class="card__body stack-sm">
              <div data-slot="col-fields" class="v-ingest-colfields"></div>
              <div class="field">
                <label class="field__label" for="ing-dr-splits">Dividers (% of page width)</label>
                <input class="input mono" id="ing-dr-splits" data-draw="splits" autocomplete="off" />
              </div>
              <div class="field">
                <label class="field__label" for="ing-dr-bbox">Table frame x0, y0, x1, y1 (fractions, y from top)</label>
                <input class="input mono" id="ing-dr-bbox" data-draw="bbox" autocomplete="off" />
              </div>
              <button type="button" class="btn btn--ghost btn--sm" data-draw="reset">${icon("refresh", { size: "sm" })}Reset layout</button>
            </div>
          </section>
          <section class="card" aria-labelledby="ing-dr-save">
            <div class="card__head"><h2 class="card__title" id="ing-dr-save">Save template</h2></div>
            <div class="card__body stack-sm">
              <div class="form-grid">
                <div class="field"><label class="field__label" for="ing-dr-bank">Bank code</label>
                  <input class="input" id="ing-dr-bank" maxlength="24" placeholder="e.g. COSMOS" autocomplete="off" /></div>
                <div class="field"><label class="field__label" for="ing-dr-name">Template name</label>
                  <input class="input" id="ing-dr-name" maxlength="120" placeholder="e.g. Cosmos current a/c 2025" autocomplete="off" /></div>
              </div>
              <button type="button" class="btn btn--primary" data-draw="save">${icon("check", { size: "sm" })}Save template</button>
              <p class="xsmall muted">Saved templates appear in the upload “PDF layout template” list for every case.</p>
            </div>
          </section>
        </div>
      </div>
      <section class="card" aria-labelledby="ing-dr-prev">
        <div class="card__head"><div><h2 class="card__title" id="ing-dr-prev">Parsed-row preview</h2>
          <p class="card__sub" data-slot="prev-sub"></p></div></div>
        <div class="card__body" data-slot="rows"></div>
      </section>
      ${(draw.preview.auto_rows_preview || []).length
        ? html`<details class="card v-ingest-auto"><summary class="card__head"><span class="card__title">Auto-detected rows (no template)</span></summary>
            <div class="card__body"><div class="table-wrap table-wrap--max"><table class="table table--compact mono xsmall"><tbody>
              ${draw.preview.auto_rows_preview.map((r) => html`<tr>${(Array.isArray(r) ? r : [r]).map((c) => html`<td>${c}</td>`)}</tr>`)}
            </tbody></table></div></div></details>`
        : ""}`
    );
    dv.svg = dv.work.querySelector("[data-svg]");
    dv.overlay = dv.work.querySelector("[data-overlay]");
    bindWorkspace();
    rebuildOverlay();
    paintRaster();
  }

  // -- overlay (frame + dividers); rebuilt only when the divider count changes, otherwise patched
  function rebuildOverlay(focusSel = null) {
    const { pw, ph } = pageSize();
    const sw = (pw / 595).toFixed(2);
    const n = draw.splits.length;
    render(
      dv.overlay,
      html`<rect class="v-ingest-bbox" data-bbox></rect>
        <rect class="v-ingest-newframe" data-newframe x="0" y="0" width="0" height="0"></rect>
        ${["x0", "x1"].map((k) => html`<line class="v-ingest-handle" data-edge="${k}" stroke-width="${(14 * sw).toFixed(1)}" tabindex="0" role="slider"
          aria-label="${k === "x0" ? "Table frame left edge" : "Table frame right edge"}" aria-orientation="horizontal" aria-valuemin="0" aria-valuemax="100"></line>`)}
        ${["y0", "y1"].map((k) => html`<line class="v-ingest-handle" data-edge="${k}" stroke-width="${(14 * sw).toFixed(1)}" tabindex="0" role="slider"
          aria-label="${k === "y0" ? "Table frame top edge" : "Table frame bottom edge"}" aria-orientation="vertical" aria-valuemin="0" aria-valuemax="100"></line>`)}
        ${Array.from({ length: n }, (_, i) => html`<line class="v-ingest-split" data-split-line="${i}"></line>
          <line class="v-ingest-handle v-ingest-handle--split" data-split="${i}" stroke-width="${(12 * sw).toFixed(1)}" tabindex="0" role="slider"
            aria-label="Column divider ${i + 1}" aria-orientation="horizontal" aria-valuemin="0" aria-valuemax="100"></line>`)}
        ${Array.from({ length: n + 1 }, (_, i) => html`<text class="v-ingest-collabel" data-col-label="${i}"></text>`)}`
    );
    patchOverlay();
    drawColumnFields();
    if (focusSel) dv.overlay.querySelector(focusSel)?.focus();
  }

  function patchOverlay() {
    const { pw, ph } = pageSize();
    const [x0, y0, x1, y1] = draw.bbox;
    const X0 = x0 * pw;
    const X1 = x1 * pw;
    const Y0 = y0 * ph;
    const Y1 = y1 * ph;
    const set = (node, attrs) => {
      if (node) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, typeof v === "number" ? v.toFixed(1) : v);
    };
    set(dv.overlay.querySelector("[data-bbox]"), { x: X0, y: Y0, width: X1 - X0, height: Y1 - Y0 });
    set(dv.overlay.querySelector('[data-edge="x0"]'), { x1: X0, x2: X0, y1: Y0, y2: Y1, "aria-valuenow": pct1(x0), "aria-valuetext": `${pct1(x0)}% from left` });
    set(dv.overlay.querySelector('[data-edge="x1"]'), { x1: X1, x2: X1, y1: Y0, y2: Y1, "aria-valuenow": pct1(x1), "aria-valuetext": `${pct1(x1)}% from left` });
    set(dv.overlay.querySelector('[data-edge="y0"]'), { x1: X0, x2: X1, y1: Y0, y2: Y0, "aria-valuenow": pct1(y0), "aria-valuetext": `${pct1(y0)}% from top` });
    set(dv.overlay.querySelector('[data-edge="y1"]'), { x1: X0, x2: X1, y1: Y1, y2: Y1, "aria-valuenow": pct1(y1), "aria-valuetext": `${pct1(y1)}% from top` });
    draw.splits.forEach((s, i) => {
      const X = s * pw;
      set(dv.overlay.querySelector(`[data-split-line="${i}"]`), { x1: X, x2: X, y1: Y0, y2: Y1 });
      set(dv.overlay.querySelector(`[data-split="${i}"]`), { x1: X, x2: X, y1: Y0, y2: Y1, "aria-valuenow": pct1(s), "aria-valuetext": `${pct1(s)}% of page width` });
    });
    const bounds = [x0, ...draw.splits, x1];
    for (let i = 0; i < bounds.length - 1; i += 1) {
      const lab = dv.overlay.querySelector(`[data-col-label="${i}"]`);
      if (!lab) continue;
      set(lab, { x: ((bounds[i] + bounds[i + 1]) / 2) * pw, y: Math.max(10, Y0 - 5) });
      lab.textContent = COLUMN_FIELDS.find((f) => f.value === draw.names[i])?.label || draw.names[i] || `Col ${i + 1}`;
    }
    syncTextInputs();
    scheduleRows();
  }

  function syncTextInputs(skip = null) {
    const sp = dv.work.querySelector('[data-draw="splits"]');
    const bb = dv.work.querySelector('[data-draw="bbox"]');
    if (sp && sp !== skip && document.activeElement !== sp) sp.value = draw.splits.map((s) => pct1(s)).join(", ");
    if (bb && bb !== skip && document.activeElement !== bb) bb.value = draw.bbox.map((v) => (Math.round(v * 1000) / 1000).toString()).join(", ");
  }

  function drawColumnFields() {
    const host = dv.work.querySelector('[data-slot="col-fields"]');
    const n = draw.splits.length + 1;
    while (draw.names.length < n) draw.names.push("ignore");
    draw.names.length = n;
    render(
      host,
      html`${Array.from({ length: n }, (_, i) => html`<div class="field">
        <label class="field__label" for="ing-dr-col-${i}">Column ${i + 1}</label>
        <select class="select select--sm" id="ing-dr-col-${i}" data-col="${i}">
          ${COLUMN_FIELDS.map((f) => html`<option value="${f.value}" ${draw.names[i] === f.value ? html`selected` : ""}>${f.label}</option>`)}
        </select></div>`)}`
    );
  }

  /** Normalise constraints: frame inside the page, dividers sorted and inside the frame. */
  function constrain() {
    let [x0, y0, x1, y1] = draw.bbox.map((v) => clamp(Number(v) || 0, 0, 1));
    if (x1 - x0 < 0.05) x1 = Math.min(1, x0 + 0.05);
    if (y1 - y0 < 0.05) y1 = Math.min(1, y0 + 0.05);
    draw.bbox = [x0, y0, x1, y1];
    draw.splits = draw.splits.map((s) => clamp(s, x0 + MIN_GAP, x1 - MIN_GAP)).sort((a, b) => a - b);
  }

  function moveTarget(target, value) {
    if (target.split != null) {
      const i = target.split;
      const lo = i === 0 ? draw.bbox[0] : draw.splits[i - 1];
      const hi = i === draw.splits.length - 1 ? draw.bbox[2] : draw.splits[i + 1];
      draw.splits[i] = clamp(value, lo + MIN_GAP, hi - MIN_GAP);
    } else {
      const b = [...draw.bbox];
      if (target.edge === "x0") b[0] = clamp(value, 0, Math.min(b[2] - 0.05, (draw.splits[0] ?? b[2]) - MIN_GAP));
      else if (target.edge === "x1") b[2] = clamp(value, Math.max(b[0] + 0.05, (draw.splits[draw.splits.length - 1] ?? b[0]) + MIN_GAP), 1);
      else if (target.edge === "y0") b[1] = clamp(value, 0, b[3] - 0.05);
      else b[3] = clamp(value, b[1] + 0.05, 1);
      draw.bbox = b;
    }
    patchOverlay();
  }

  const targetOf = (node) => (node.dataset.split != null ? { split: Number(node.dataset.split) } : { edge: node.dataset.edge });
  const valueOf = (t) => (t.split != null ? draw.splits[t.split] : draw.bbox[{ x0: 0, y0: 1, x1: 2, y1: 3 }[t.edge]]);

  function svgPoint(e) {
    const ctm = dv.svg.getScreenCTM();
    if (!ctm) return null;
    const p = dv.svg.createSVGPoint();
    p.x = e.clientX;
    p.y = e.clientY;
    return p.matrixTransform(ctm.inverse());
  }

  function bindWorkspace() {
    const work = dv.work;
    let drag = null;
    const offs = [
      delegate(work, "pointerdown", "[data-split], [data-edge]", (e, h) => {
        e.preventDefault();
        h.focus();
        drag = { target: targetOf(h), node: h, id: e.pointerId };
        h.setPointerCapture?.(e.pointerId);
        h.classList.add("is-dragging");
      }),
      delegate(work, "pointermove", "[data-split], [data-edge]", (e) => {
        if (!drag || e.pointerId !== drag.id) return;
        const p = svgPoint(e);
        if (!p) return;
        const { pw, ph } = pageSize();
        const vertical = drag.target.edge === "y0" || drag.target.edge === "y1";
        moveTarget(drag.target, vertical ? p.y / ph : p.x / pw);
      }),
      delegate(work, "keydown", "[data-split], [data-edge]", (e, h) => {
        const t = targetOf(h);
        const vertical = t.edge === "y0" || t.edge === "y1";
        const step = e.shiftKey ? 0.02 : 0.005;
        let d = 0;
        if (e.key === (vertical ? "ArrowUp" : "ArrowLeft")) d = -step;
        else if (e.key === (vertical ? "ArrowDown" : "ArrowRight")) d = step;
        else if (e.key === "ArrowUp" || e.key === "ArrowLeft") d = -step;
        else if (e.key === "ArrowDown" || e.key === "ArrowRight") d = step;
        else if ((e.key === "Delete" || e.key === "Backspace") && t.split != null) {
          e.preventDefault();
          draw.splits.splice(t.split, 1);
          draw.names.splice(t.split + 1, 1);
          rebuildOverlay(`[data-split="${Math.max(0, t.split - 1)}"]`);
          return;
        }
        if (!d) return;
        e.preventDefault();
        moveTarget(t, valueOf(t) + d);
      }),
      delegate(work, "change", '[data-draw="page"]', (e, s) => {
        draw.page = Number(s.value) || 1;
        buildWorkspace();
        loadRaster();
      }),
      delegate(work, "change", '[data-draw="tokens"]', (e, box) => {
        draw.showTokens = box.checked;
        syncTokenLayer();
      }),
      delegate(work, "change", "[data-col]", (e, s) => {
        draw.names[Number(s.dataset.col)] = s.value;
        patchOverlay();
      }),
      delegate(work, "change", '[data-draw="splits"]', (e, inp) => {
        const vals = inp.value.split(/[,\s]+/).filter(Boolean).map(Number);
        const bad = !vals.length || vals.some((v) => !Number.isFinite(v) || v <= 0 || v >= 100);
        inp.setAttribute("aria-invalid", bad ? "true" : "false");
        if (bad) {
          toast.warn("Dividers must be numbers between 0 and 100, separated by commas.");
          return;
        }
        const countChanged = vals.length !== draw.splits.length;
        draw.splits = vals.map((v) => v / 100);
        constrain();
        if (countChanged) rebuildOverlay();
        else patchOverlay();
      }),
      delegate(work, "change", '[data-draw="bbox"]', (e, inp) => {
        const vals = inp.value.split(/[,\s]+/).filter(Boolean).map(Number);
        const bad = vals.length !== 4 || vals.some((v) => !Number.isFinite(v) || v < 0 || v > 1);
        inp.setAttribute("aria-invalid", bad ? "true" : "false");
        if (bad) {
          toast.warn("The frame needs four fractions between 0 and 1: x0, y0, x1, y1.");
          return;
        }
        draw.bbox = vals;
        constrain();
        patchOverlay();
      }),
      delegate(work, "click", '[data-draw="add"]', () => {
        const bounds = [draw.bbox[0], ...draw.splits, draw.bbox[2]];
        let best = 0;
        for (let i = 1; i < bounds.length - 1; i += 1) if (bounds[i + 1] - bounds[i] > bounds[best + 1] - bounds[best]) best = i;
        const at = (bounds[best] + bounds[best + 1]) / 2;
        draw.splits.splice(best, 0, at);
        draw.names.splice(best + 1, 0, "ignore");
        constrain();
        rebuildOverlay(`[data-split="${best}"]`);
      }),
      delegate(work, "click", '[data-draw="remove"]', () => {
        if (!draw.splits.length) return;
        draw.splits.pop();
        draw.names.pop();
        rebuildOverlay();
      }),
      delegate(work, "click", '[data-draw="reset"]', () => {
        draw.bbox = [...DRAW_DEFAULTS.bbox];
        draw.splits = [...DRAW_DEFAULTS.splits];
        draw.names = [...DRAW_DEFAULTS.names];
        rebuildOverlay();
      }),
      delegate(work, "click", '[data-draw="save"]', (e, b) => saveTemplate(b)),
    ];
    // drawing a new frame: press on the page (not on a handle) and drag; the dividers keep their relative
    // positions inside the frame. A click without a drag changes nothing.
    let newFrame = null;
    const ghost = () => dv.overlay.querySelector("[data-newframe]");
    const startFrame = (e) => {
      if (e.button !== 0 || drag || !dv.svg || !dv.svg.contains(e.target) || e.target.closest("[data-split], [data-edge]")) return;
      const p = svgPoint(e);
      if (!p) return;
      const { pw, ph } = pageSize();
      e.preventDefault();
      newFrame = { id: e.pointerId, x: clamp(p.x / pw, 0, 1), y: clamp(p.y / ph, 0, 1), old: [...draw.bbox], oldSplits: [...draw.splits], applied: false };
      dv.svg.setPointerCapture?.(e.pointerId);
      dv.svg.classList.add("is-drawing");
    };
    const moveFrame = (e) => {
      if (!newFrame || e.pointerId !== newFrame.id) return;
      const p = svgPoint(e);
      if (!p) return;
      const { pw, ph } = pageSize();
      const cx = clamp(p.x / pw, 0, 1);
      const cy = clamp(p.y / ph, 0, 1);
      const x0 = Math.min(newFrame.x, cx);
      const x1 = Math.max(newFrame.x, cx);
      const y0 = Math.min(newFrame.y, cy);
      const y1 = Math.max(newFrame.y, cy);
      const g = ghost();
      if (g) {
        g.classList.add("is-on");
        for (const [k, v] of Object.entries({ x: x0 * pw, y: y0 * ph, width: (x1 - x0) * pw, height: (y1 - y0) * ph })) g.setAttribute(k, v.toFixed(1));
      }
      if (x1 - x0 < MIN_FRAME || y1 - y0 < MIN_FRAME) return;
      const [ox0, , ox1] = newFrame.old;
      const span = ox1 - ox0 || 1;
      draw.bbox = [x0, y0, x1, y1];
      draw.splits = newFrame.oldSplits.map((s) => x0 + ((s - ox0) / span) * (x1 - x0));
      constrain();
      newFrame.applied = true;
      patchOverlay();
    };
    const endFrame = (e) => {
      if (!newFrame || (e.pointerId != null && e.pointerId !== newFrame.id)) return;
      ghost()?.classList.remove("is-on");
      dv.svg?.classList.remove("is-drawing");
      if (newFrame.applied) {
        const [x0, y0, x1, y1] = draw.bbox;
        toast(`Table frame set to ${pct1(x0)}–${pct1(x1)}% across, ${pct1(y0)}–${pct1(y1)}% down.`, { timeout: 2200 });
      }
      newFrame = null;
    };
    const endDrag = (e) => {
      endFrame(e);
      if (!drag || (e.pointerId != null && e.pointerId !== drag.id)) return;
      drag.node.classList.remove("is-dragging");
      drag = null;
    };
    work.addEventListener("pointerdown", startFrame);
    work.addEventListener("pointermove", moveFrame);
    work.addEventListener("pointerup", endDrag);
    work.addEventListener("pointercancel", endDrag);
    offs.push(() => {
      work.removeEventListener("pointerdown", startFrame);
      work.removeEventListener("pointermove", moveFrame);
      work.removeEventListener("pointerup", endDrag);
      work.removeEventListener("pointercancel", endDrag);
    });
    // workspace is rebuilt on page change / new preview: drop the old listeners then
    if (dv.unbind) dv.unbind();
    dv.unbind = () => offs.forEach((f) => f());
  }
  ctx.onCleanup(() => dv?.unbind?.());

  /** Client-side replica of the parser's row grouping, for a live preview of the layout. */
  function computeRows() {
    const { pw, ph } = pageSize();
    const [x0, y0, x1, y1] = draw.bbox;
    const splitsPt = draw.splits.map((s) => s * pw);
    const inside = pageTokens()
      .map((t) => ({ x: Number(t.x), xEnd: Number(t.x_end) || 0, yTop: ph - Number(t.y), text: String(t.text) }))
      .filter((t) => t.x >= x0 * pw && t.x <= x1 * pw && t.yTop >= y0 * ph && t.yTop <= y1 * ph)
      .sort((a, b) => a.yTop - b.yTop || a.x - b.x);
    const lines = [];
    for (const t of inside) {
      const last = lines[lines.length - 1];
      if (!last || Math.abs(last.y - t.yTop) > 3.5) lines.push({ y: t.yTop, toks: [t] });
      else last.toks.push(t);
    }
    return lines.map((l) => {
      const row = Array(splitsPt.length + 1).fill("");
      l.toks
        .sort((a, b) => a.x - b.x)
        .forEach((t) => {
          // user-drawn dividers: right-aligned amounts are placed by their right edge (parser numeric_anchor="right")
          const anchor = t.xEnd > t.x && RIGHT_NUMERIC.test(t.text.trim()) ? t.xEnd - 0.01 : t.x;
          const c = splitsPt.filter((s) => s <= anchor).length;
          row[c] = row[c] ? `${row[c]} ${t.text}` : t.text;
        });
      return row;
    });
  }

  function scheduleRows() {
    cancelAnimationFrame(drawRaf);
    drawRaf = requestAnimationFrame(drawRows);
  }

  function drawRows() {
    const host = dv?.work.querySelector('[data-slot="rows"]');
    if (!host) return;
    const rows = computeRows();
    const sub = dv.work.querySelector('[data-slot="prev-sub"]');
    if (sub) sub.textContent = `${plural(rows.length, "line")} inside the frame on page ${draw.page}; first 15 shown as the parser will split them.`;
    if (!rows.length) {
      render(host, html`<p class="small muted">No text inside the table frame. Enlarge the frame or check the page.</p>`);
      return;
    }
    const names = draw.names.map((n) => COLUMN_FIELDS.find((f) => f.value === n)?.label || n);
    const numeric = new Set(["debit", "credit", "balance", "amount"]);
    render(
      host,
      html`<div class="table-wrap"><table class="table table--compact v-ingest-rows">
        <thead><tr>${names.map((n, i) => html`<th scope="col" class="${numeric.has(draw.names[i]) ? "num" : ""} ${draw.names[i] === "ignore" ? "muted" : ""}">${n}</th>`)}</tr></thead>
        <tbody>${rows.slice(0, 15).map((r) => html`<tr>${r.map((c, i) => html`<td class="${numeric.has(draw.names[i]) ? "num" : ""} ${draw.names[i] === "ignore" ? "muted" : ""}">${c}</td>`)}</tr>`)}</tbody>
      </table></div>`
    );
  }

  async function saveTemplate(btn) {
    const bank = dv.work.querySelector("#ing-dr-bank");
    const name = dv.work.querySelector("#ing-dr-name");
    const bankCode = bank.value.trim().toUpperCase();
    if (!bankCode) {
      bank.setAttribute("aria-invalid", "true");
      bank.focus();
      toast.warn("Enter a bank code (e.g. COSMOS) for the template.");
      return;
    }
    bank.setAttribute("aria-invalid", "false");
    constrain();
    // FR-ING-04: same field-map rules as the server (one date column, an amount column, no duplicate fields)
    const mapped = draw.names.filter((n) => n && n !== "ignore");
    const dupes = [...new Set(mapped.filter((n, i) => mapped.indexOf(n) !== i))];
    const fieldLabel = (v) => COLUMN_FIELDS.find((f) => f.value === v)?.label || v;
    let mapError = "";
    if (draw.names.length !== draw.splits.length + 1) mapError = "Every column needs a field.";
    else if (!mapped.includes("date")) mapError = "Map one column to Txn date.";
    else if (!mapped.some((n) => n === "debit" || n === "credit" || n === "amount")) mapError = "Map a Debit, Credit or Amount column.";
    else if (dupes.length) mapError = `${dupes.map(fieldLabel).join(", ")} is mapped to more than one column.`;
    if (mapError) {
      dv.work.querySelector('[data-slot="col-fields"] select')?.focus();
      toast.warn(mapError, { title: "Check the column fields" });
      return;
    }
    const tpl = await withBusy(
      btn,
      () =>
        api.post("/api/draw-templates", {
          bank_code: bankCode,
          template_name: name.value.trim() || `${bankCode} custom layout`,
          table_bbox: draw.bbox.map((v) => Math.round(v * 10000) / 10000),
          column_splits: draw.splits.map((v) => Math.round(v * 10000) / 10000),
          column_names: draw.names,
          case_id: caseId,
        }),
      { error: "Could not save template" }
    );
    if (!tpl) return;
    await store.loadMeta().catch(() => null);
    store.bump([TOPICS.templates]);
    if (!ctx.isCurrent()) return;
    templateId = tpl.template_id || "";
    drawTemplates();
    ctx.toast.success(`${tpl.bank_code}: ${tpl.template_name} saved and selected for the next upload.`, {
      title: "Template saved",
      action: { label: "Go to upload", run: () => tabs.select("upload", { focus: true }) },
    });
  }

  // ===================================================================================== data sync
  const syncFromCase = (d) => {
    if (!d || !ctx.isCurrent()) return;
    stmtGrid?.setRows(d.statements || []);
    historyGrid?.setRows(d.recent_ingest_results || []);
    tabs.setBadge("statements", int((d.statements || []).length));
    if (!running && d.latest_agent_telemetry) {
      lastTelemetry = d.latest_agent_telemetry;
      drawTelemetry();
    }
    drawStmtSummary();
  };
  ctx.onCleanup(store.select((s) => s.caseData, syncFromCase));
  ctx.onData([TOPICS.statements], () => {
    provenance = null;
  });
  ctx.onData([TOPICS.templates], drawTemplates);
  ctx.onData([TOPICS.llm], drawModeHint);
  ctx.onCleanup(store.select((s) => s.health, () => ctx.isCurrent() && drawModeHint()));

  // the upload panel owns the batch options other tabs reference (template select): build it first
  ensurePanel("upload", tabs.panel("upload"));
  ensurePanel(tabs.selected, tabs.panel(tabs.selected));

  ctx.registerCommands([
    { id: "ingest.add", title: "Add statement files…", group: "Ingest", icon: "upload", keywords: ["upload", "pdf", "excel"],
      run: () => { tabs.select("upload"); up?.input.click(); } },
    { id: "ingest.run", title: "Ingest queued files", group: "Ingest", icon: "play", when: () => !running && queue.some((q) => SENDABLE.has(q.status)), run: () => runIngest() },
    { id: "ingest.statements", title: "Show statements & custody hashes", group: "Ingest", icon: "hash", run: () => tabs.select("statements", { focus: true }) },
    { id: "ingest.draw", title: "Open PDF draw tool", group: "Ingest", icon: "table", keywords: ["template", "layout", "calibrate"], run: () => tabs.select("draw", { focus: true }) },
    { id: "ingest.agents", title: "Run AI backup agents (S2–S4)", group: "Ingest", icon: "cpu",
      run: () => { tabs.select("upload"); runAgents(up?.panel.querySelector('[data-action="run-agents"]')); } },
  ]);

  return {
    update(params) {
      const t = ["upload", "statements", "draw"].includes(params.tab) ? params.tab : "upload";
      if (t !== tabs.selected) tabs.select(t);
    },
  };
}
