/* Client-generated files (CSV exports, SVG/PNG snapshots): one CSV cell encoder with a spreadsheet
 * formula-injection guard, and one Blob save helper. Server files go through api.js downloadBlob(). */

/** One CSV cell: numbers raw, arrays joined with "|", text quoted when needed and guarded against
 * spreadsheet formula injection (a leading = + - @ tab or CR gets a ' prefix, except plain signed numbers). */
export function csvCell(v) {
  if (v == null) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  if (typeof v === "boolean") return v ? "true" : "false";
  let s = Array.isArray(v) ? v.map((x) => (x == null ? "" : String(x))).join("|") : String(v);
  // plain signed numbers ("-12.50") are data, not formulas
  if (/^[=+\-@\t\r]/.test(s) && !/^[-+]?\d+(\.\d+)?$/.test(s)) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** CSV text from rows of cells (CRLF line ends, UTF-8 BOM so Excel detects the encoding). */
export function toCsv(rows) {
  return `﻿${rows.map((cells) => cells.map(csvCell).join(",")).join("\r\n")}`;
}

/** A file-system-safe file name. */
export const safeFilename = (name) => String(name || "download").replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_");

/** Save a Blob as a download via a temporary object URL. Returns { filename, size }. */
export function saveBlob(blob, filename, { revokeAfterMs = 60_000 } = {}) {
  const name = safeFilename(filename);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), revokeAfterMs);
  return { filename: name, size: blob.size };
}

/** Save client-generated text (SVG, CSV, …). Returns { filename, size }. */
export function downloadText(filename, text, type = "text/plain;charset=utf-8") {
  return saveBlob(new Blob([text], { type }), filename);
}

/** Save rows of cells as a .csv file. Returns { filename, size }. */
export function downloadCsv(filename, rows) {
  return downloadText(filename, toCsv(rows), "text/csv;charset=utf-8");
}
