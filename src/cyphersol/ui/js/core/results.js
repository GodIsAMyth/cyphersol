/* Interpreters for structured server results, shared so every view reports them the same way. */

/**
 * Human summary of POST /api/cases/{id}/edge/tally-push. Branches on res.success (a reachable Tally that
 * created nothing is NOT success), res.connected, res.message and the first of res.line_errors.
 * Returns { ok, tone: "success"|"warn"|"error", title, text }.
 */
export function describeTallyPushResult(result) {
  const res = result || {};
  if (res.success) {
    return {
      ok: true,
      tone: "success",
      title: "Tally sync complete",
      text: `Created ${res.created ?? 0}, altered ${res.altered ?? 0}, errors ${res.errors ?? 0}.`,
    };
  }
  let text = res.message || "No details returned.";
  const firstLineError = Array.isArray(res.line_errors) && res.line_errors.length ? String(res.line_errors[0]) : "";
  if (firstLineError && !text.includes(firstLineError)) text += ` First error: ${firstLineError}`;
  if (res.connected && typeof res.created === "number") text += ` (Created ${res.created}, errors ${res.errors ?? 0})`;
  return {
    ok: false,
    tone: res.connected ? "error" : "warn",
    title: res.connected ? "Tally sync failed" : "Tally not reachable",
    text,
  };
}

/** Summary of an upload response ({ingested:[{filename,status,error}], files_ok, total_case_transactions}). */
export function describeUploadResult(res, mode) {
  const items = (res && res.ingested) || [];
  const failed = items.filter((r) => r.status === "failed");
  const ok = res?.files_ok ?? items.length - failed.length;
  return {
    ok: failed.length === 0,
    tone: failed.length === 0 ? "success" : ok > 0 ? "warn" : "error",
    title: failed.length === 0 ? "Statements ingested" : ok > 0 ? "Partially ingested" : "Ingest failed",
    text:
      `Processed ${ok} file(s)${mode ? ` in ${mode} mode` : ""}. Case now has ${res?.total_case_transactions ?? "?"} transactions.` +
      (failed.length ? ` ${failed.length} failed: ${failed.map((f) => `${f.filename} (${f.error || "error"})`).join("; ")}.` : ""),
    failed,
  };
}

/** Summary of agent telemetry (upload result agent_telemetry / overview.latest_agent_telemetry). */
export function describeAgentTelemetry(tel) {
  if (!tel) return null;
  const sd = tel.stage_details || {};
  const parts = [];
  if (sd.stage1?.invoked) parts.push(`S1 vision: ${sd.stage1.files_recovered || 0} file(s)`);
  if (sd.stage2?.invoked) parts.push(`S2 reconciler: ${sd.stage2.rows_repaired || 0} row(s)`);
  if (sd.stage3?.invoked) parts.push(`S3 rail/party: ${sd.stage3.rows_enriched || 0} row(s)`);
  if (sd.stage4?.invoked) {
    parts.push(`S4 category/entity: ${sd.stage4.transactions_classified || 0} classified, ${sd.stage4.entities_merged || 0} merged`);
  }
  return {
    mode: tel.execution_mode || "",
    summary: parts.length ? parts.join(" · ") : "All rows handled by heuristics (no LLM fallback triggered).",
    warnings: tel.warnings || [],
    stagesInvoked: tel.stages_invoked || [],
  };
}
