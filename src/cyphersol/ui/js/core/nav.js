/* View registry: names, titles, icons, which workspace (case mode) shows them, nav badges.
 * The router only imports views listed here (views/<name>.js). Adding a view = add an entry + the module. */

export const VIEWS = {
  // global (no case in the URL)
  cases: { title: "Cases", icon: "folder", scope: "global", desc: "All workspaces and investigations" },
  settings: { title: "Settings", icon: "settings", scope: "global", desc: "Runtime mode, AI providers & keys, appearance" },
  // case-scoped, shared by both products
  overview: { title: "Overview", icon: "overview", scope: "case", desc: "Case summary, statements and audit trail" },
  ingest: { title: "Ingest", icon: "upload", scope: "case", desc: "Upload statements, draw tool, custody hashes" },
  transactions: { title: "Transactions", icon: "list", scope: "case", desc: "All categorised transactions" },
  suspense: { title: "Suspense", icon: "inbox", scope: "case", desc: "Resolve uncategorised entries" },
  anomalies: { title: "Anomalies", icon: "alert", scope: "case", desc: "Data-quality gate and flagged rows" },
  entities: { title: "Entities", icon: "users", scope: "case", desc: "Entity resolution and merges" },
  // CypherEdge
  tally: { title: "Tally", icon: "file", scope: "case", product: "cypher_edge", desc: "Tally XML export and push" },
  itr: { title: "ITR", icon: "receipt", scope: "case", product: "cypher_edge", desc: "ITR-ready compliance summary" },
  loan: { title: "Loan", icon: "bank", scope: "case", product: "cypher_edge", desc: "Loan eligibility, ABB and FOIR" },
  // CypherTrace
  network: { title: "Network", icon: "network", scope: "case", product: "cypher_trace", desc: "Fund-flow network graph" },
  bidirectional: { title: "Bidirectional", icon: "swap", scope: "case", product: "cypher_trace", desc: "Mutual flows between two parties" },
  layering: { title: "Layering", icon: "layers", scope: "case", product: "cypher_trace", desc: "Multi-hop money flow tree" },
  loops: { title: "Loops", icon: "repeat", scope: "case", product: "cypher_trace", desc: "Circular transaction loops" },
  mules: { title: "Mules", icon: "shield", scope: "case", product: "cypher_trace", desc: "10-parameter mule risk scoring" },
  profile: { title: "Profiles", icon: "user", scope: "case", product: "cypher_trace", desc: "Individual financial profile" },
  // internal
  notfound: { title: "Not found", icon: "alert", scope: "hidden", desc: "Unknown address" },
};

export const VIEW_NAMES = Object.keys(VIEWS);
export const isView = (name) => Object.prototype.hasOwnProperty.call(VIEWS, name);
export const viewTitle = (name) => VIEWS[name]?.title || name;

/** Primary workspace navigation per case mode (order matters). */
export const NAV_BY_MODE = {
  cypher_edge: ["overview", "ingest", "transactions", "suspense", "anomalies", "tally", "itr", "loan"],
  cypher_trace: [
    "overview", "ingest", "transactions", "suspense", "anomalies",
    "network", "bidirectional", "layering", "loops", "mules", "entities", "profile",
  ],
};

/** Case views of the other product, offered under a collapsible "More" section. */
export function moreViews(mode) {
  const primary = new Set(NAV_BY_MODE[mode] || NAV_BY_MODE.cypher_trace);
  return VIEW_NAMES.filter((n) => VIEWS[n].scope === "case" && !primary.has(n));
}

export const primaryViews = (mode) => NAV_BY_MODE[mode] || NAV_BY_MODE.cypher_trace;

/** Sidebar count badges derived from overview.summary. Return null to hide. */
export const NAV_BADGES = {
  anomalies: (s) => (s && s.anomaly_count ? { count: s.anomaly_count, tone: "danger", label: "flagged rows" } : null),
  suspense: (s) => (s && s.suspense_count ? { count: s.suspense_count, tone: "warn", label: "pending" } : null),
};
