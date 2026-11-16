/* Small observable app store + data actions shared by the shell and every view.
 *
 * state = {
 *   meta,            // GET /api/meta (categories, banks, provider_catalog, agent_definitions, draw_templates, …)
 *   health,          // GET /api/health (cloud_egress_possible, effective_execution_mode, …)
 *   llmConfig,       // GET /api/llm/config (public dict; never contains raw keys)
 *   cases,           // GET /api/cases -> cases[] ({case_id, title, mode, agency_or_firm_name, …})
 *   caseId, case, mode,          // current case (mode: "cypher_edge" | "cypher_trace")
 *   caseData,        // GET /api/cases/{id}/overview?tx_limit=0 — statements, entities, summary, audit_logs,
 *                    //   near_merge_candidates, recent_ingest_results, latest_agent_telemetry, revision
 *   caseStatus,      // "idle" | "loading" | "ready" | "error"
 *   caseError,       // ApiError when caseStatus === "error"
 *   summary,         // caseData.summary (statements_count, transactions_count, suspense_count, anomaly_count, …)
 *   versions,        // { [topic]: n } bumped by mutated(); views refetch when a topic they use changes
 * }
 *
 * Topics (use TOPICS.*): transactions, suspense, anomalies, entities, statements, analytics, llm, templates, cases.
 */
import { api, caseUrl, isAbort } from "./api.js";
import { STORAGE_KEYS, lsSet } from "./constants.js";

export const TOPICS = Object.freeze({
  transactions: "transactions",
  suspense: "suspense",
  anomalies: "anomalies",
  entities: "entities",
  statements: "statements",
  analytics: "analytics", // graph, layering, loops, mules, profile, bidirectional, tally/itr/loan
  llm: "llm",
  templates: "templates",
  cases: "cases",
});

/** Which topics a typical mutation touches; pass to store.mutated(). */
export const MUTATION_TOPICS = Object.freeze({
  upload: ["statements", "transactions", "suspense", "anomalies", "entities", "analytics"],
  resolveSuspense: ["transactions", "suspense", "analytics"],
  toggleDuplicate: ["transactions", "anomalies", "suspense", "analytics"],
  recluster: ["entities", "transactions", "analytics"],
  layeringMisc: ["analytics"],
  runAgents: ["statements", "transactions", "suspense", "anomalies", "entities", "analytics"],
  llmConfig: ["llm"],
  template: ["templates"],
});

function createStore(initial) {
  let state = { ...initial };
  const subs = new Set();
  return {
    get: () => state,
    set(patch) {
      const changed = [];
      const next = { ...state };
      for (const [k, v] of Object.entries(patch)) {
        if (next[k] !== v) {
          next[k] = v;
          changed.push(k);
        }
      }
      if (!changed.length) return;
      state = next;
      subs.forEach((fn) => {
        try {
          fn(state, changed);
        } catch (err) {
          console.error(err);
        }
      });
    },
    /** fn(state, changedKeys) on every change. Returns unsubscribe. */
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}

const base = createStore({
  meta: null,
  health: null,
  llmConfig: null,
  cases: [],
  caseId: null,
  case: null,
  mode: null,
  caseData: null,
  caseStatus: "idle",
  caseError: null,
  summary: null,
  versions: {},
});

let caseGen = 0;
let caseController = null;
let caseDataPromise = null;

export const store = {
  get: base.get,
  set: base.set,
  subscribe: base.subscribe,

  /** Call fn(value, state) when selector(state) changes (===). Fires once immediately if fireNow. */
  select(selector, fn, { fireNow = false } = {}) {
    let prev = selector(base.get());
    if (fireNow) fn(prev, base.get());
    return base.subscribe((s) => {
      const v = selector(s);
      if (v !== prev) {
        prev = v;
        fn(v, s);
      }
    });
  },

  // ------------------------------------------------------------------ global data
  async loadMeta() {
    const meta = await api.get("/api/meta");
    base.set({ meta, llmConfig: base.get().llmConfig || meta.llm_config || null });
    return meta;
  },
  async loadHealth() {
    try {
      const health = await api.get("/api/health");
      base.set({ health });
      return health;
    } catch (err) {
      base.set({ health: null });
      throw err;
    }
  },
  async loadLlmConfig() {
    const llmConfig = await api.get("/api/llm/config");
    base.set({ llmConfig });
    return llmConfig;
  },
  /** Save LLM/runtime settings (POST /api/llm/config); refreshes health so the cloud banner stays honest. */
  async saveLlmConfig(payload) {
    const llmConfig = await api.post("/api/llm/config", payload);
    base.set({ llmConfig });
    store.bump([TOPICS.llm]);
    store.loadHealth().catch(() => {});
    return llmConfig;
  },
  async loadCases() {
    const res = await api.get("/api/cases");
    const cases = res.cases || [];
    const cur = base.get().caseId;
    const patch = { cases };
    if (cur) patch.case = cases.find((c) => c.case_id === cur) || base.get().case;
    base.set(patch);
    return cases;
  },
  async createCase({ title, mode, agency_or_firm_name }) {
    const created = await api.post("/api/cases", { title, mode, agency_or_firm_name });
    await store.loadCases();
    store.bump([TOPICS.cases]);
    return created;
  },

  // ------------------------------------------------------------------ current case
  /**
   * Make caseId current and load its caseData. Resolves to caseData (or throws ApiError, e.g. 404).
   * Re-selecting the current, already-loaded case is a no-op. A newer setCase() aborts an older one.
   */
  async setCase(caseId) {
    const s = base.get();
    if (!caseId) {
      base.set({ caseId: null, case: null, mode: null, caseData: null, summary: null, caseStatus: "idle", caseError: null });
      return null;
    }
    if (s.caseId === caseId && s.caseStatus === "ready") return s.caseData;
    if (s.caseId === caseId && s.caseStatus === "loading" && caseDataPromise) return caseDataPromise;
    const known = s.cases.find((c) => c.case_id === caseId) || null;
    base.set({
      caseId,
      case: known,
      mode: known ? known.mode : null,
      caseData: null,
      summary: null,
      caseStatus: "loading",
      caseError: null,
    });
    lsSet(STORAGE_KEYS.lastCase, caseId);
    return store.refreshCaseData();
  },

  /** Re-fetch caseData for the current case (lightweight overview: tx_limit=0). */
  refreshCaseData() {
    const caseId = base.get().caseId;
    if (!caseId) return Promise.resolve(null);
    caseGen += 1;
    const gen = caseGen;
    if (caseController) caseController.abort();
    caseController = new AbortController();
    const { signal } = caseController;
    caseDataPromise = api
      .get(caseUrl(caseId, "overview"), { params: { tx_limit: 0 }, signal })
      .then((data) => {
        if (gen !== caseGen) return data;
        base.set({
          caseData: data,
          case: data.case || base.get().case,
          mode: (data.case && data.case.mode) || base.get().mode,
          summary: data.summary || null,
          caseStatus: "ready",
          caseError: null,
          llmConfig: data.llm_config || base.get().llmConfig,
        });
        return data;
      })
      .catch((err) => {
        if (isAbort(err)) throw err;
        if (gen === caseGen) base.set({ caseStatus: "error", caseError: err });
        throw err;
      });
    return caseDataPromise;
  },

  /** caseData for the current case, loading it if needed. */
  ensureCaseData() {
    const s = base.get();
    if (s.caseStatus === "ready" && s.caseData) return Promise.resolve(s.caseData);
    if (s.caseStatus === "loading" && caseDataPromise) return caseDataPromise;
    return store.refreshCaseData();
  },

  // ------------------------------------------------------------------ invalidation
  /** Bump topic versions (no refetch). */
  bump(topics) {
    const versions = { ...base.get().versions };
    for (const t of topics) versions[t] = (versions[t] || 0) + 1;
    base.set({ versions });
  },
  /**
   * Call after any successful mutation: refreshes the case summary/entities (sidebar badges, pickers) and
   * THEN bumps the topics, so onTopics() listeners already see fresh caseData. Resolves when done.
   * opts.caseId: the case the mutation belongs to. When the user has since switched to another case the
   * refresh and the topic bump are skipped (they would describe the wrong case). A case load already in
   * flight is never aborted: the refresh is chained after it.
   */
  async mutated(topics, { refreshCase = true, caseId = undefined } = {}) {
    const target = caseId === undefined ? base.get().caseId : caseId;
    if (target && target !== base.get().caseId) return;
    if (refreshCase && target) {
      if (base.get().caseStatus === "loading" && caseDataPromise) await caseDataPromise.catch(() => null);
      if (base.get().caseId !== target) return;
      await store.refreshCaseData().catch(() => null);
      if (base.get().caseId !== target) return;
    }
    store.bump(topics);
  },
  version: (topic) => base.get().versions[topic] || 0,
  /** fn(topic) whenever any of the topics is bumped. Returns unsubscribe. */
  onTopics(topics, fn) {
    let last = topics.map((t) => store.version(t));
    return base.subscribe((s, changed) => {
      if (!changed.includes("versions")) return;
      topics.forEach((t, i) => {
        const v = s.versions[t] || 0;
        if (v !== last[i]) {
          last[i] = v;
          fn(t);
        }
      });
    });
  },

  // ------------------------------------------------------------------ taxonomy helpers
  categories: () => (base.get().meta && base.get().meta.categories) || [],
  category: (id) => store.categories().find((c) => Number(c.category_id) === Number(id)) || null,
  categoryLabel(id) {
    const c = store.category(id);
    return c ? `${String(c.category_id).padStart(2, "0")} — ${c.name}` : String(id ?? "");
  },
  /** Real (non-system, non UNRESOLVED_*) entities of the current case, from caseData. */
  entities() {
    const ents = (base.get().caseData && base.get().caseData.entities) || [];
    return ents.filter((e) => !e.is_system && !String(e.canonical_name || "").startsWith("UNRESOLVED_"));
  },
  entity: (id) => ((base.get().caseData && base.get().caseData.entities) || []).find((e) => e.entity_id === id) || null,
  statements: () => (base.get().caseData && base.get().caseData.statements) || [],
};
