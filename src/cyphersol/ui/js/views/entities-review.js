/* Pure (DOM-free) helpers for the entity cluster review (FR-ENT-01, report NLP-11 / NLP-12 / UI-S-14).
 *
 *   previewSummary(preview, nameOf)   GET entities/preview?threshold= -> merges / splits / conflicts in words
 *   conflictMessage(conflict)         HOLDER_GUARD / CANNOT_LINK / PAN_AMBIGUOUS -> plain-language reason
 *   holderGuardWarning(a, b)          warning shown before a manual merge of two statement holders
 *   decisionText(decision, nameOf)    one-line description of a stored merge / split / reject decision
 *   evidenceText(ev)                  why two spellings were linked (NAME similarity, ACCOUNT, VPA, PAN…)
 *   candidatePairs(preview|overview)  near-miss pairs with IDs, deduplicated and sorted by similarity
 * Tested under node by tests/test_p1_ui_data.py.
 */
import { num, plural } from "../core/format.js";

const OP_LABEL = { merge: "Merge", split: "Split", reject: "Not the same" };
export const opLabel = (op) => OP_LABEL[op] || String(op || "");

/** Why the resolver refused to link two names automatically. */
export function conflictMessage(c) {
  const a = c?.a || "?";
  const b = c?.b || "?";
  switch (c?.type) {
    case "HOLDER_GUARD":
      return `Holder guard: "${a}" and "${b}" are the holders of different statements, so they are kept apart automatically even though the names are similar. Merge them manually only if both statements belong to the same person.`;
    case "CANNOT_LINK":
      return `Rejected pair: an analyst marked "${a}" and "${b}" as different parties, so they are never linked again.`;
    case "PAN_AMBIGUOUS": {
      const pan = c?.detail && typeof c.detail === "object" ? c.detail.pan : "";
      return `Ambiguous PAN${pan ? ` ${pan}` : ""}: it appears next to several different names (often the holder's own PAN in TDS / GST narrations), so it was not used to link "${a}" and "${b}".`;
    }
    default:
      return `"${a}" and "${b}" were not linked (${String(c?.type || "conflict").toLowerCase().replace(/_/g, " ")}).`;
  }
}

/** Badge tone for a conflict type. */
export const conflictTone = (type) => (type === "HOLDER_GUARD" ? "warn" : type === "CANNOT_LINK" ? "neutral" : "info");
export const conflictLabel = (type) => ({ HOLDER_GUARD: "Holder guard", CANNOT_LINK: "Rejected pair", PAN_AMBIGUOUS: "Ambiguous PAN" }[type] || String(type || "Conflict"));

/** Why two spellings were clustered. */
export function evidenceText(ev) {
  const reason = String(ev?.reason || "").toUpperCase();
  const d = ev?.detail;
  if (reason === "NAME") return `name similarity ${typeof d === "number" ? num(d, 2) : ""}`.trim();
  if (reason === "ACCOUNT") return `same account${d ? ` ${typeof d === "object" ? d.account || "" : d}` : ""}`;
  if (reason === "VPA") return `same VPA${d ? ` ${typeof d === "object" ? d.vpa || "" : d}` : ""}`;
  if (reason === "PAN") return `same PAN${d ? ` ${typeof d === "object" ? d.pan || "" : d}` : ""}`;
  if (reason === "STATEMENT_ACCOUNT") return `statement holder's account${d ? ` ${d}` : ""}`;
  if (reason === "STATEMENT_PAN") return `statement holder's PAN${d ? ` ${d}` : ""}`;
  if (reason === "MANUAL" || reason === "DECISION") return "analyst merge";
  return reason ? reason.toLowerCase().replace(/_/g, " ") : "linked";
}

/** Shown before a manual merge when both sides hold their own statement (the holder guard is overridden). */
export function holderGuardWarning(a, b) {
  if (!a || !b || !a.has_statement || !b.has_statement) return null;
  return `Both "${a.canonical_name}" and "${b.canonical_name}" hold statements in this case. The holder guard keeps different account holders apart; a manual merge overrides it and treats both statements as one person's.`;
}

/**
 * Plain summary of a threshold preview, measured against the stored entities.
 * nameOf(id) -> display name (falls back to the preview's own cluster names, then a short id).
 */
export function previewSummary(preview, nameOf = () => "") {
  const p = preview || {};
  const clusters = new Map((p.clusters || []).map((c) => [c.entity_id, c]));
  const name = (id) => nameOf(id) || clusters.get(id)?.canonical_name || `entity ${String(id || "").slice(0, 8)}`;
  const ch = p.changes || {};
  const merged = (ch.merged || []).map((m) => {
    const others = (m.from_entity_ids || []).filter((x) => x !== m.entity_id);
    return {
      entity_id: m.entity_id,
      name: clusters.get(m.entity_id)?.canonical_name || name(m.entity_id),
      from: (m.from_entity_ids || []).map(name),
      absorbed: others.map(name),
      text: `${(m.from_entity_ids || []).map(name).join(" + ")} → one entity`,
    };
  });
  const split = (ch.split || []).map((s) => ({
    entity_id: s.entity_id,
    name: name(s.entity_id),
    into: (s.into_entity_ids || []).map((x) => clusters.get(x)?.canonical_name || name(x)),
    text: `${name(s.entity_id)} → ${plural((s.into_entity_ids || []).length, "entity", "entities")}`,
  }));
  const conflicts = (p.conflicts || []).map((c) => ({ ...c, message: conflictMessage(c), label: conflictLabel(c.type), tone: conflictTone(c.type) }));
  const nothing = !merged.length && !split.length;
  return {
    threshold: p.threshold,
    entityCount: p.entity_count,
    multiAlias: p.multi_alias_clusters,
    merged,
    split,
    conflicts,
    holderGuards: conflicts.filter((c) => c.type === "HOLDER_GUARD"),
    retired: (ch.retired_entity_ids || []).length,
    created: (ch.new_entity_ids || []).length,
    unchanged: nothing,
    headline: nothing
      ? "No entity would change at this threshold."
      : [merged.length ? `${plural(merged.length, "merge")}` : "", split.length ? `${plural(split.length, "split")}` : ""].filter(Boolean).join(", ") + " compared with the current entities.",
  };
}

/** One-line description of a decision ({op, groups, entity_ids, note}). */
export function decisionText(d, nameOf = () => "") {
  const g = d?.groups || [];
  const names = (grp) => (grp || []).join(", ");
  const ids = d?.entity_ids || [];
  if (d?.op === "merge") return `Merged ${names(g[0]) || nameOf(ids[0]) || "?"} into ${names(g[1]) || nameOf(ids[1]) || "?"}`;
  if (d?.op === "split") return `Split ${names(g[0]) || "?"} out of ${names(g[1]) || nameOf(ids[0]) || "?"}`;
  if (d?.op === "reject") return `Marked ${names(g[0]) || nameOf(ids[0]) || "?"} and ${names(g[1]) || nameOf(ids[1]) || "?"} as different parties`;
  return opLabel(d?.op);
}

/** History action -> label. */
export const historyLabel = (action) => ({ ENTITY_MERGE: "Merged", ENTITY_SPLIT: "Split", ENTITY_REJECT: "Rejected pair", ENTITY_DECISION_UNDO: "Undone" }[action] || String(action || ""));

/** Near-miss pairs with both IDs, best first, without duplicates (A⇄B == B⇄A). */
export function candidatePairs(list) {
  const seen = new Set();
  return (list || [])
    .filter((c) => c && c.entity_a_id && c.entity_b_id && c.entity_a_id !== c.entity_b_id)
    .filter((c) => {
      const k = [c.entity_a_id, c.entity_b_id].sort().join("|");
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .sort((a, b) => (Number(b.similarity) || 0) - (Number(a.similarity) || 0));
}

/** Validate a split selection: non-empty subset that leaves at least one alias behind. Returns an error or "". */
export function splitError(aliases, selected) {
  const all = new Set(aliases || []);
  const sel = (selected || []).filter((a) => all.has(a));
  if (!sel.length) return "Choose at least one alias to split off.";
  if (sel.length >= all.size) return "Leave at least one alias in the original entity.";
  return "";
}
