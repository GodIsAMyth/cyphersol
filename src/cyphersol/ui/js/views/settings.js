/* Settings (#/settings): the single source of truth for the runtime execution mode, the AI provider
 * configuration (provider, text / vision model, API key, stage 1–4 toggles, timeout, test connection,
 * clear key), an honest privacy / egress panel (key_sources, effective_execution_mode,
 * cloud_egress_possible), agent stage status, the Tally connection used by the Tally view, appearance and
 * system facts. Saves go through store.saveLlmConfig(), which refreshes /api/health so the Cloud-AI banner
 * and runtime chip stay truthful. Status panels are patched on change; the form is only repopulated after
 * an explicit save / clear / provider switch, so an analyst's unsaved edits are never overwritten. */
import { api, isAbort } from "../core/api.js";
import { AGENT_STAGES, EXEC_MODES, STORAGE_KEYS, execModeLabel, getTallyConnection, setTallyConnection } from "../core/constants.js";
import { delegate, html, render } from "../core/dom.js";
import { dateTime, int } from "../core/format.js";
import { TOPICS } from "../core/store.js";
import { getThemePref, onThemeChange, setThemePref } from "../core/theme.js";
import { icon } from "../components/icons.js";
import { confirmDialog } from "../components/modal.js";
import { pageHead, spinner } from "../components/states.js";
import { toast, withBusy } from "../components/toast.js";
import { mountLedgerMap } from "./settings-ledger.js";

export const styles = ["views/cases.css"];

/** Re-exported for older imports; the helpers live in core/constants.js. */
export const TALLY_STORAGE_KEY = STORAGE_KEYS.tally;
export { getTallyConnection };

const HOST_RE = /^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*|[0-9a-fA-F:]+)$/;
const isLoopback = (h) => /^(localhost|127(\.\d{1,3}){3}|\[?::1\]?)$/i.test(String(h).trim());

const SOURCE_LABEL = {
  vault: { label: "Saved in vault", tone: "success" },
  env: { label: "From environment", tone: "info" },
  none: { label: "No key", tone: "neutral" },
};

export function mount(el, ctx) {
  const { store } = ctx;
  let provider = (store.get().llmConfig || {}).provider || "gemini";

  const cfg = () => store.get().llmConfig || (store.get().meta && store.get().meta.llm_config) || {};
  const catalog = () => (store.get().meta && store.get().meta.provider_catalog) || {};
  const agentDefs = () => (store.get().meta && store.get().meta.agent_definitions) || [];
  const health = () => store.get().health;
  const egress = () => (health() ? Boolean(health().cloud_egress_possible) : Boolean(cfg().cloud_egress_possible));

  render(
    el,
    html`<div class="stack-lg v-settings-page">
      ${pageHead({
        title: "Settings",
        sub: "Runtime mode, AI providers and keys, integrations and appearance. These apply to every case on this machine.",
      })}
      <div data-slot="privacy" aria-live="polite"></div>

      <section class="card" aria-labelledby="st-mode">
        <div class="card__head"><div><h2 class="card__title" id="st-mode">Runtime execution mode</h2>
          <p class="card__sub">Default for every upload and agent run. The Ingest view can override it for a single batch.</p></div></div>
        <div class="card__body stack">
          <div class="v-settings-modes" role="radiogroup" aria-labelledby="st-mode" data-slot="modes"></div>
          <div data-slot="effective"></div>
        </div>
      </section>

      <section class="card" aria-labelledby="st-ai">
        <div class="card__head"><div><h2 class="card__title" id="st-ai">AI provider</h2>
          <p class="card__sub">Keys are stored in the local vault and never shown again; leave the key blank to keep the saved one.</p></div>
          <span data-slot="saved-badge"></span></div>
        <form class="card__body stack" data-slot="ai-form" novalidate autocomplete="off"></form>
      </section>

      <section class="card" aria-labelledby="st-stages">
        <div class="card__head"><div><h2 class="card__title" id="st-stages">Agent stages</h2>
          <p class="card__sub">Status of the four AI agents under the current configuration.</p></div></div>
        <div class="card__body" data-slot="stages"></div>
      </section>

      <div class="grid-2">
        <section class="card" aria-labelledby="st-tally">
          <div class="card__head"><div><h2 class="card__title" id="st-tally">Tally connection</h2>
            <p class="card__sub">Used by CypherEdge “Push to Tally”. Saved on this machine. Ledger and group mapping is set per case in “Tally ledger map” below.</p></div></div>
          <form class="card__body stack" data-slot="tally" novalidate autocomplete="off"></form>
        </section>
        <section class="card" aria-labelledby="st-look">
          <div class="card__head"><h2 class="card__title" id="st-look">Appearance</h2></div>
          <div class="card__body stack" data-slot="appearance"></div>
        </section>
      </div>

      <section class="card" aria-labelledby="st-ledger">
        <div class="card__head"><div><h2 class="card__title" id="st-ledger">Tally ledger map</h2>
          <p class="card__sub">Per case: the Tally group and ledger each category posts to, custom groups, counterparty ledgers and voucher options used by the Tally XML / XLSX exports.</p></div></div>
        <div class="card__body" data-slot="ledger"></div>
      </section>

      <section class="card" aria-labelledby="st-sys">
        <div class="card__head"><h2 class="card__title" id="st-sys">System</h2></div>
        <div class="card__body" data-slot="system"></div>
      </section>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);

  // ------------------------------------------------------------------ privacy / egress (honest status)
  function drawPrivacy() {
    const c = cfg();
    const h = health();
    const on = egress();
    const prov = (h && h.llm_provider) || c.provider;
    const src = (h && h.llm_key_source) || (c.key_sources || {})[c.provider] || "none";
    const eff = (h && h.effective_execution_mode) || c.effective_execution_mode || "heuristics";
    render(
      slot("privacy"),
      on
        ? html`<div class="callout callout--warn" role="status">${icon("cloud")}<div class="stack-sm">
            <div><strong>Cloud AI is enabled.</strong> An API key for ${label(prov)} is active (${SOURCE_LABEL[src]?.label || src}).
              Effective default mode: <strong>${execModeLabel(eff)}</strong>.</div>
            <div class="small">Statement content may be sent to this external provider whenever an AI mode is used.
              Choose “Heuristics only” or clear the key to keep all processing on this machine.</div></div></div>`
        : html`<div class="callout callout--success" role="status">${icon("shield")}<div>
            <strong>Offline.</strong> No cloud AI key is active, so no statement data can leave this machine. All extraction and
            categorisation runs on local heuristics.</div></div>`
    );
  }

  const label = (p) => (catalog()[p] && catalog()[p].label) || p || "provider";

  // ------------------------------------------------------------------ execution mode
  function drawModes() {
    const c = cfg();
    const mode = c.execution_mode || "heuristics";
    render(
      slot("modes"),
      html`${EXEC_MODES.map(
        (m) => html`<button type="button" class="v-settings-mode" role="radio" data-mode="${m.value}"
            aria-checked="${m.value === mode ? "true" : "false"}" tabindex="${m.value === mode ? "0" : "-1"}">
          <span class="v-settings-mode__head">${icon(m.value === "heuristics" ? "cpu" : m.value === "llm" ? "cloud" : "layers")}
            <span class="strong">${m.label}</span>${m.value === "heuristics" ? html`<span class="badge badge--success">Default · offline</span>` : ""}</span>
          <span class="small text-2">${m.desc}</span>
        </button>`
      )}`
    );
    const eff = c.effective_execution_mode || mode;
    const degraded = eff !== mode;
    render(
      slot("effective"),
      html`<dl class="kv">
        <dt>Configured</dt><dd>${execModeLabel(mode)}</dd>
        <dt>Effective</dt><dd>${execModeLabel(eff)}
          ${degraded ? html` <span class="badge badge--warn">Degraded</span> <span class="small muted">— no API key for ${label(c.provider)}, so AI modes fall back to heuristics.</span>` : ""}</dd>
        <dt>Cloud egress</dt><dd>${egress() ? html`<span class="badge badge--warn">${icon("cloud", { size: "sm" })}Possible</span>` : html`<span class="badge badge--success">${icon("shield", { size: "sm" })}None</span>`}</dd>
      </dl>`
    );
  }

  async function setMode(mode, btn) {
    if (mode === cfg().execution_mode) return;
    if (mode !== "heuristics" && cfg().has_api_key) {
      const ok = await confirmDialog({
        title: `Switch to ${execModeLabel(mode)}?`,
        message: `Rows the heuristics cannot handle${mode === "llm" ? " — and in LLM-primary mode, every stage —" : ""} will be sent to ${label(cfg().provider)} on future uploads and agent runs.`,
        confirmLabel: "Switch mode",
      });
      if (!ok || !ctx.isCurrent()) return;
    }
    const res = await withBusy(btn, () => store.saveLlmConfig({ execution_mode: mode }), { error: "Could not change the mode" });
    if (res && ctx.isCurrent()) {
      toast.success(`Runtime mode: ${execModeLabel(res.execution_mode)}${res.effective_execution_mode !== res.execution_mode ? ` (effective: ${execModeLabel(res.effective_execution_mode)})` : ""}.`);
      el.querySelector(`[data-mode="${res.execution_mode}"]`)?.focus();
    }
  }

  // ------------------------------------------------------------------ provider form
  function drawForm({ keepProvider = false } = {}) {
    const c = cfg();
    if (!keepProvider) provider = c.provider || provider;
    const p = catalog()[provider] || { text_models: [], vision_models: [], default_model: "", default_vision_model: "" };
    const sameProv = provider === c.provider;
    const model = sameProv ? c.model || p.default_model : p.default_model;
    const vision = sameProv ? c.vision_model || p.default_vision_model : p.default_vision_model;
    const masked = (c.masked_keys_by_provider || {})[provider] || "";
    const src = (c.key_sources || {})[provider] || (masked ? "vault" : "none");
    const disabledEnv = (c.disabled_env_providers || []).includes(provider);
    render(
      slot("ai-form"),
      html`<div class="form-grid">
          <div class="field">
            <label class="field__label" for="st-provider">Provider</label>
            <select class="select" id="st-provider" name="provider">
              ${Object.values(catalog()).map((x) => html`<option value="${x.id}" ${x.id === provider ? html`selected` : ""}>${x.label}${x.id === c.provider ? " (active)" : ""}</option>`)}
            </select>
          </div>
          <div class="field">
            <label class="field__label" for="st-model">Text model</label>
            <input class="input mono" id="st-model" name="model" list="st-models-text" value="${model}" spellcheck="false" />
            <datalist id="st-models-text">${(p.text_models || []).map((m) => html`<option value="${m}"></option>`)}</datalist>
          </div>
          <div class="field">
            <label class="field__label" for="st-vision">Vision model (Stage 1 OCR)</label>
            <input class="input mono" id="st-vision" name="vision_model" list="st-models-vision" value="${vision}" spellcheck="false" />
            <datalist id="st-models-vision">${(p.vision_models || []).map((m) => html`<option value="${m}"></option>`)}</datalist>
          </div>
        </div>
        <div class="field">
          <label class="field__label" for="st-key">API key for ${p.label || provider}</label>
          <div class="row-wrap">
            <input class="input mono v-settings-key" id="st-key" name="api_key" type="password" autocomplete="new-password" spellcheck="false"
              placeholder="${masked ? "Leave blank to keep the saved key" : p.key_hint || "Paste API key"}" aria-describedby="st-key-hint" />
            <span class="badge badge--${SOURCE_LABEL[src]?.tone || "neutral"} badge--lg">${icon("key", { size: "sm" })}${masked ? html`<span class="mono">${masked}</span> · ` : ""}${SOURCE_LABEL[src]?.label || src}</span>
          </div>
          <span class="field__hint" id="st-key-hint">${p.env_var ? html`Environment fallback: <span class="mono">${p.env_var}</span>${c.cloud_env_opt_in ? " (opt-in enabled)" : " (ignored unless the cloud opt-in is set)"}. ` : ""}${disabledEnv ? "The environment key for this provider is suppressed because it was cleared here." : ""}</span>
        </div>
        <fieldset class="v-settings-stages">
          <legend class="field__label">Enabled agent stages</legend>
          ${AGENT_STAGES.map((s) => {
            const def = agentDefs().find((d) => d.stage === s.stage) || {};
            return html`<label class="v-settings-stage">
              <input type="checkbox" name="${s.key}" ${c[s.key] ?? true ? html`checked` : ""} />
              <span><span class="strong">${def.title || s.label}</span><span class="xsmall muted">${def.subtitle || ""}</span></span>
            </label>`;
          })}
        </fieldset>
        <div class="field v-settings-timeout">
          <label class="field__label" for="st-timeout">Request timeout (seconds)</label>
          <input class="input input--num input--w-sm" id="st-timeout" name="timeout_sec" type="number" min="5" max="600" step="1" value="${c.timeout_sec ?? 45}" />
        </div>
        <div data-slot="test-result" aria-live="polite"></div>
        <div class="row-wrap v-settings-actions">
          <button type="submit" class="btn btn--primary">${icon("check", { size: "sm" })}Save provider settings</button>
          <button type="button" class="btn" data-action="test">${icon("zap", { size: "sm" })}Test connection</button>
          <span class="spacer"></span>
          <button type="button" class="btn btn--ghost" data-action="clear-key" ${masked || src !== "none" ? "" : html`disabled`}>${icon("trash", { size: "sm" })}Clear saved key</button>
        </div>`
    );
    render(
      slot("saved-badge"),
      c.has_api_key
        ? html`<span class="badge badge--warn">${icon("cloud", { size: "sm" })}${label(c.provider)} active</span>`
        : html`<span class="badge badge--neutral">Not configured</span>`
    );
  }

  function formPayload() {
    const f = slot("ai-form");
    const v = {
      provider,
      model: f.querySelector("#st-model").value.trim(),
      vision_model: f.querySelector("#st-vision").value.trim(),
      timeout_sec: Math.max(5, Math.min(600, Number(f.querySelector("#st-timeout").value) || 45)),
    };
    for (const s of AGENT_STAGES) v[s.key] = Boolean(f.querySelector(`[name="${s.key}"]`)?.checked);
    const key = f.querySelector("#st-key").value.trim();
    if (key) v.api_key = key;
    return v;
  }

  async function save(btn) {
    const payload = formPayload();
    const keyGiven = Boolean(payload.api_key);
    if (keyGiven && cfg().execution_mode !== "heuristics") {
      const ok = await confirmDialog({
        title: "Enable cloud AI?",
        message: `With this key and the ${execModeLabel(cfg().execution_mode)} mode, statement content can be sent to ${label(provider)}.`,
        confirmLabel: "Save key",
      });
      if (!ok || !ctx.isCurrent()) return;
    }
    const res = await withBusy(btn, () => store.saveLlmConfig(payload), { error: "Could not save settings" });
    if (!res || !ctx.isCurrent()) return;
    drawForm();
    toast.success(
      keyGiven ? `Settings saved. ${label(res.provider)} key stored in the vault.` : `Settings saved (${label(res.provider)}).`,
      { title: "AI provider" }
    );
  }

  async function test(btn) {
    const p = formPayload();
    const out = slot("ai-form").querySelector('[data-slot="test-result"]');
    render(out, html`<div class="callout callout--info small">${spinner("Testing")}<div>Contacting ${label(provider)}…</div></div>`);
    const res = await withBusy(
      btn,
      () => api.post("/api/llm/test", { provider, model: p.model, vision_model: p.vision_model, api_key: p.api_key }, { signal: ctx.signal }),
      { silent: true }
    ).catch(() => null);
    if (!ctx.isCurrent()) return;
    if (!res) {
      render(out, html`<div class="callout callout--danger small" role="alert">${icon("x-circle", { size: "sm" })}<div>The test request failed. Check that CypherSOL is running.</div></div>`);
      return;
    }
    render(
      out,
      res.ok
        ? html`<div class="callout callout--success small">${icon("check-circle", { size: "sm" })}<div>Connected to <strong>${label(res.provider)}</strong> (<span class="mono">${res.model}</span>)${res.latency_ms != null ? html` in <strong class="tabular">${int(res.latency_ms)} ms</strong>` : ""}.${p.api_key ? " The key is not saved until you press Save." : ""}</div></div>`
        : html`<div class="callout callout--danger small" role="alert">${icon("x-circle", { size: "sm" })}<div>Connection failed: ${res.error || "unknown error"}</div></div>`
    );
  }

  async function clearKey(btn) {
    const ok = await confirmDialog({
      title: `Clear the ${label(provider)} key?`,
      message: "The saved key is removed from the vault and any environment key for this provider is ignored. AI modes fall back to heuristics until a new key is saved.",
      confirmLabel: "Clear key",
      danger: true,
    });
    if (!ok || !ctx.isCurrent()) return;
    const res = await withBusy(btn, () => store.saveLlmConfig({ provider, clear_api_key: true }), { error: "Could not clear the key" });
    if (!res || !ctx.isCurrent()) return;
    drawForm();
    toast.success(`Cleared the saved key for ${label(provider)}.`);
  }

  // ------------------------------------------------------------------ agent stage status (G11)
  function drawStages() {
    const c = cfg();
    const tel = (store.get().caseData && store.get().caseData.latest_agent_telemetry) || null;
    const invoked = new Set(((tel && tel.stages_invoked) || []).map(Number));
    const sd = (tel && tel.stage_details) || {};
    const status = (s) => {
      if (c.execution_mode === "heuristics") return { text: "Heuristics only", tone: "neutral" };
      if (!(c[s.key] ?? true)) return { text: "Disabled", tone: "neutral" };
      if (!c.has_api_key) return { text: "Needs API key", tone: "warn" };
      if (invoked.has(s.stage)) {
        const d = sd[`stage${s.stage}`] || {};
        const n = [d.files_recovered, d.rows_repaired, d.rows_enriched, d.transactions_classified][s.stage - 1] || 0;
        return { text: `Invoked (${int(n)})`, tone: "info" };
      }
      return c.execution_mode === "llm" ? { text: "Primary active", tone: "success" } : { text: "Backup ready", tone: "success" };
    };
    render(
      slot("stages"),
      html`<div class="table-wrap"><table class="table table--compact">
        <caption class="sr-only">Agent stage status</caption>
        <thead><tr><th scope="col">Stage</th><th scope="col">Agent</th><th scope="col">Uses</th><th scope="col">Status</th></tr></thead>
        <tbody>${AGENT_STAGES.map((s) => {
          const def = agentDefs().find((d) => d.stage === s.stage) || {};
          const st = status(s);
          return html`<tr><td class="strong">S${s.stage}</td><td>${s.short}<div class="xsmall muted">${def.subtitle || s.label}</div></td>
            <td class="small">${def.uses_vision ? html`<span class="mono">${c.vision_model || "—"}</span>` : html`<span class="mono">${c.model || "—"}</span>`}</td>
            <td><span class="badge badge--${st.tone}">${st.text}</span></td></tr>`;
        })}</tbody></table></div>
      ${tel ? html`<p class="xsmall muted">Invocation counts from the current case's last pipeline run${tel.timestamp ? ` (${dateTime(tel.timestamp)})` : ""}.</p>` : ""}`
    );
  }

  // ------------------------------------------------------------------ Tally connection
  function drawTally() {
    const t = getTallyConnection();
    render(
      slot("tally"),
      html`<div class="form-grid">
          <div class="field"><label class="field__label" for="st-tally-host">Host</label>
            <input class="input mono" id="st-tally-host" name="host" value="${t.host}" spellcheck="false" aria-describedby="st-tally-hint" /></div>
          <div class="field"><label class="field__label" for="st-tally-port">Port</label>
            <input class="input input--num mono" id="st-tally-port" name="port" type="number" min="1" max="65535" value="${t.port}" /></div>
        </div>
        <div class="field"><label class="field__label" for="st-tally-company">Company name <span class="muted">(optional)</span></label>
          <input class="input" id="st-tally-company" name="company_name" value="${t.company_name || ""}" maxlength="120" placeholder="As it appears in Tally" aria-describedby="st-tally-company-hint" />
          <span class="field__hint" id="st-tally-company-hint">Reference only. The company each case's vouchers go to is the ledger map's company name (Tally → Ledger map…, or the Tally ledger map card below), or the one typed on the Tally page.</span></div>
        <span class="field__hint" id="st-tally-hint">Loopback (127.0.0.1 / localhost) is always allowed. Any other host must be listed in the server's <span class="mono">CYPHERSOL_TALLY_HOSTS</span> environment variable, otherwise pushes are refused.</span>
        <div data-slot="tally-warn"></div>
        <div class="row-wrap"><button type="submit" class="btn btn--primary">${icon("check", { size: "sm" })}Save connection</button>
          <button type="button" class="btn btn--ghost" data-action="tally-reset">Reset to 127.0.0.1:9000</button></div>`
    );
    tallyWarn();
  }
  function tallyWarn() {
    const host = slot("tally").querySelector("#st-tally-host")?.value.trim() || "";
    render(
      slot("tally").querySelector('[data-slot="tally-warn"]'),
      host && !isLoopback(host)
        ? html`<div class="callout callout--warn small">${icon("alert", { size: "sm" })}<div>${host} is not a loopback address; the server rejects it unless allow-listed.</div></div>`
        : ""
    );
  }
  function saveTally() {
    const f = slot("tally");
    const hostEl = f.querySelector("#st-tally-host");
    const portEl = f.querySelector("#st-tally-port");
    const host = hostEl.value.trim();
    const port = Number(portEl.value);
    const hostOk = HOST_RE.test(host);
    const portOk = Number.isInteger(port) && port > 0 && port < 65536;
    hostEl.setAttribute("aria-invalid", hostOk ? "false" : "true");
    portEl.setAttribute("aria-invalid", portOk ? "false" : "true");
    if (!hostOk || !portOk) {
      toast.warn(!hostOk ? "Enter a valid host name or IP address." : "Port must be between 1 and 65535.", { title: "Tally connection" });
      (hostOk ? portEl : hostEl).focus();
      return;
    }
    setTallyConnection({ host, port, company_name: f.querySelector("#st-tally-company").value.trim() });
    toast.success(`Tally pushes will target ${host}:${port}.`, { title: "Tally connection saved" });
  }

  // ------------------------------------------------------------------ appearance
  function drawAppearance() {
    const pref = getThemePref();
    const opts = [
      { value: "light", label: "Light", icon: "sun" },
      { value: "dark", label: "Dark", icon: "moon" },
      { value: "system", label: "Match system", icon: "monitor" },
    ];
    render(
      slot("appearance"),
      html`<div class="field"><span class="field__label" id="st-theme-l">Theme</span>
        <div class="seg" role="radiogroup" aria-labelledby="st-theme-l">
          ${opts.map((o) => html`<button type="button" class="seg__btn" role="radio" data-theme-opt="${o.value}"
            aria-checked="${pref === o.value ? "true" : "false"}" tabindex="${pref === o.value ? "0" : "-1"}">${icon(o.icon, { size: "sm" })}${o.label}</button>`)}
        </div></div>
        <p class="small muted">Shortcuts: <kbd>⌘K</kbd> / <kbd>Ctrl K</kbd> command palette · <kbd>[</kbd> toggle the sidebar.</p>`
    );
  }

  // ------------------------------------------------------------------ system facts
  function drawSystem() {
    const m = store.get().meta || {};
    const h = health() || {};
    render(
      slot("system"),
      html`<dl class="kv v-settings-sys">
        <dt>Engine</dt><dd>${h.engine || "CypherSOL"} <span class="mono">${m.version || h.version || ""}</span></dd>
        <dt>Parser version</dt><dd class="mono xsmall">${m.parser_version || "—"}</dd>
        <dt>Banks supported</dt><dd class="tabular">${int(m.banks_supported_count ?? h.banks_supported)}</dd>
        <dt>Categories</dt><dd class="tabular">${int(m.categories_count ?? h.categories)}</dd>
        <dt>Knowledge base</dt><dd class="tabular">${int(m.knowledge_base_data_points ?? h.knowledge_base_points)} curated terms</dd>
        <dt>Vault encryption</dt><dd>${m.vault_encrypted ? html`<span class="badge badge--success">${icon("lock", { size: "sm" })}Encrypted</span>` : html`<span class="badge badge--warn">Not encrypted</span> <span class="small muted">— protect the vault file with disk encryption.</span>`}</dd>
        <dt>PDF templates</dt><dd class="tabular">${int((m.draw_templates || []).length)} custom</dd>
      </dl>`
    );
  }

  // ------------------------------------------------------------------ events
  const radioKeys = (selector) => (e, b) => {
    if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(e.key)) return;
    e.preventDefault();
    const btns = Array.from(el.querySelectorAll(selector));
    const i = btns.indexOf(b) + (e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1);
    const next = btns[(i + btns.length) % btns.length];
    next.focus();
    next.click();
  };
  const offs = [
    delegate(el, "click", "[data-mode]", (e, b) => setMode(b.dataset.mode, b)),
    delegate(el, "keydown", "[data-mode]", radioKeys("[data-mode]")),
    delegate(el, "change", "#st-provider", (e, s) => {
      provider = s.value;
      drawForm({ keepProvider: true });
      el.querySelector("#st-provider")?.focus();
    }),
    delegate(el, "submit", '[data-slot="ai-form"]', (e) => {
      e.preventDefault();
      save(e.target.querySelector('button[type="submit"]'));
    }),
    delegate(el, "click", '[data-action="test"]', (e, b) => test(b)),
    delegate(el, "click", '[data-action="clear-key"]', (e, b) => clearKey(b)),
    delegate(el, "submit", '[data-slot="tally"]', (e) => {
      e.preventDefault();
      saveTally();
    }),
    delegate(el, "input", "#st-tally-host", tallyWarn),
    delegate(el, "click", '[data-action="tally-reset"]', () => {
      setTallyConnection(null);
      drawTally();
      toast("Tally connection reset to 127.0.0.1:9000.", { timeout: 2500 });
    }),
    delegate(el, "click", "[data-theme-opt]", (e, b) => setThemePref(b.dataset.themeOpt)),
    delegate(el, "keydown", "[data-theme-opt]", radioKeys("[data-theme-opt]")),
  ];
  ctx.onCleanup(() => offs.forEach((f) => f()));
  ctx.onCleanup(onThemeChange(() => ctx.isCurrent() && drawAppearance()));

  // live status: patch panels, never the form being edited
  const drawStatus = () => {
    if (!ctx.isCurrent()) return;
    drawPrivacy();
    drawModes();
    drawStages();
    drawSystem();
  };
  ctx.onCleanup(store.select((s) => s.health, drawStatus));
  ctx.onCleanup(store.select((s) => s.llmConfig, drawStatus));
  ctx.onCleanup(
    store.select(
      (s) => s.meta,
      () => {
        if (!ctx.isCurrent()) return;
        drawSystem();
        // the catalog arrived late: the form was drawn without provider lists
        if (!slot("ai-form").querySelector("#st-provider option")) drawForm();
      }
    )
  );
  ctx.onData([TOPICS.templates], drawSystem);

  ctx.registerCommands([
    { id: "settings.heuristics", title: "Runtime: heuristics only (offline)", group: "Settings", icon: "cpu",
      when: () => cfg().execution_mode !== "heuristics", run: () => setMode("heuristics", null) },
    { id: "settings.test", title: "Test AI provider connection", group: "Settings", icon: "zap", run: () => test(el.querySelector('[data-action="test"]')) },
    { id: "settings.tally", title: "Edit Tally connection", group: "Settings", icon: "file", run: () => el.querySelector("#st-tally-host")?.focus() },
  ]);

  drawPrivacy();
  drawModes();
  drawForm();
  drawStages();
  drawTally();
  drawAppearance();
  drawSystem();
  mountLedgerMap(slot("ledger"), ctx);

  // make sure the page shows the server's current truth (another tab may have changed it)
  Promise.all([store.loadLlmConfig(), store.loadHealth()])
    .then(() => {
      if (ctx.isCurrent() && !slot("ai-form").contains(document.activeElement)) drawForm();
    })
    .catch((err) => {
      if (!isAbort(err) && ctx.isCurrent()) toast.error(err, { title: "Could not load AI settings" });
    });
}
