/* The ONLY network entry point of the UI (tests assert exactly one call to window fetch across ui/js).
 *
 * P0 security contract:
 *   - every request carries the per-launch token from <meta name="cyphersol-token"> in X-CypherSOL-Token;
 *   - request bodies are JSON with Content-Type: application/json;
 *   - the token is never placed in a URL; exports are fetched with the header and saved as Blobs.
 */

export const TOKEN_HEADER = "X-CypherSOL-Token";

/** Per-launch API token injected by the local server into the page. */
export const API_TOKEN = document.querySelector('meta[name="cyphersol-token"]')?.getAttribute("content") || "";

const FRIENDLY = {
  0: "Cannot reach the local CypherSOL server. Is it still running?",
  400: "The request was rejected as invalid.",
  401: "Not authorised.",
  403: "This session is no longer authorised (the server was restarted). Reload the page to continue.",
  404: "Not found. It may have been deleted or belong to another case.",
  409: "This changed in the meantime (conflict). Refresh and try again.",
  411: "The request was rejected (length required).",
  413: "The upload is too large for one request.",
  415: "The request was sent in an unsupported format.",
  500: "The server hit an internal error.",
};

/** Typed API error. `message` is the user-facing text (server detail when safe, else a friendly default). */
export class ApiError extends Error {
  constructor(status, serverMessage, { errorId = null, path = "", body = null } = {}) {
    const friendly = FRIENDLY[status] || (status >= 500 ? FRIENDLY[500] : `Request failed (${status}).`);
    // 400/404/409/413 server messages are written for users (validation text); 403/500 are not shown raw.
    const useServer = serverMessage && [400, 404, 409, 413].includes(status);
    super(useServer ? serverMessage : friendly);
    this.name = "ApiError";
    this.status = status;
    this.serverMessage = serverMessage || "";
    this.friendly = friendly;
    this.errorId = errorId;
    this.path = path;
    this.body = body;
  }
  get isAuth() {
    return this.status === 401 || this.status === 403;
  }
  get isNotFound() {
    return this.status === 404;
  }
  get isConflict() {
    return this.status === 409;
  }
  get isValidation() {
    return this.status === 400;
  }
  get isServer() {
    return this.status >= 500;
  }
  get isNetwork() {
    return this.status === 0;
  }
}

/** True for errors caused by AbortController.abort() (navigation, superseded request). Never toast these. */
export const isAbort = (err) => err?.name === "AbortError" || err?.cause?.name === "AbortError";

/** Build a query string, skipping null/undefined/"" values. qs({a:1,b:null}) -> "?a=1". */
export function qs(params) {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, String(x)));
    else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** "/api/cases/{id}/{sub}" with the id encoded. */
export const caseUrl = (caseId, sub = "") =>
  `/api/cases/${encodeURIComponent(caseId)}${sub ? `/${String(sub).replace(/^\/+/, "")}` : ""}`;

/**
 * Low-level request. Returns the Response (ok only); throws ApiError (or an AbortError).
 * options: { method, body (object -> JSON), params (query object), signal, headers }
 */
export async function apiFetch(path, { method, body, params, signal, headers: extra } = {}) {
  const headers = new Headers(extra || {});
  headers.set(TOKEN_HEADER, API_TOKEN);
  let payload;
  if (body !== undefined) {
    payload = typeof body === "string" ? body : JSON.stringify(body);
    headers.set("Content-Type", "application/json");
  }
  const url = path + qs(params);
  let res;
  try {
    res = await fetch(url, {
      method: method || (body !== undefined ? "POST" : "GET"),
      headers,
      body: payload,
      signal,
      credentials: "same-origin",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    });
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ApiError(0, "", { path });
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new ApiError(res.status, data?.error || "", { errorId: data?.error_id || null, path, body: data });
  }
  return res;
}

/** GET JSON. api.get("/api/meta"), api.get(caseUrl(id, "transactions"), { params: { limit: 200 }, signal }) */
async function get(path, opts = {}) {
  const res = await apiFetch(path, { ...opts, method: "GET" });
  return res.json();
}

/** POST JSON body, returns parsed JSON. */
async function post(path, body = {}, opts = {}) {
  const res = await apiFetch(path, { ...opts, method: "POST", body });
  return res.json();
}

export const api = { get, post, fetch: apiFetch };

/**
 * Case-scoped client: const c = caseApi(caseId, signal); await c.get("trace/mules"); await c.post("…", body).
 * The signal (if any) is applied to every call unless overridden per call.
 */
export function caseApi(caseId, signal) {
  return {
    caseId,
    url: (sub, params) => caseUrl(caseId, sub) + qs(params),
    get: (sub, params, opts = {}) => get(caseUrl(caseId, sub), { signal, ...opts, params }),
    post: (sub, body = {}, opts = {}) => post(caseUrl(caseId, sub), body, { signal, ...opts }),
    download: (sub, fallbackName, opts = {}) => downloadBlob(caseUrl(caseId, sub), fallbackName, { signal, ...opts }),
  };
}

function filenameFromDisposition(res, fallback) {
  const cd = res.headers.get("Content-Disposition") || "";
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  if (star) {
    try {
      return decodeURIComponent(star[1]);
    } catch (_e) {
      /* fall through */
    }
  }
  const m = /filename="?([^";]+)"?/i.exec(cd);
  return (m ? m[1] : fallback).replace(/[\\/:*?"<>|]+/g, "_");
}

/**
 * Fetch a file with the token header and save it via a Blob object URL (no token in URLs, and an error
 * never replaces the SPA with a JSON page — UI-S-27). Resolves { filename, size, type }.
 * opts: { params, signal, open: false } — open: true opens the blob in a new tab instead of saving
 * (note: blob: documents inherit this page's CSP, so pages relying on inline styles render unstyled).
 */
export async function downloadBlob(path, fallbackName = "download", { params, signal, open = false } = {}) {
  const res = await apiFetch(path, { params, signal });
  const blob = await res.blob();
  const filename = filenameFromDisposition(res, fallbackName);
  const url = URL.createObjectURL(blob);
  if (open) {
    // "noopener" makes window.open return null even on success, so there is nothing to check here.
    window.open(url, "_blank", "noopener");
  } else {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return { filename, size: blob.size, type: blob.type };
}

/** Read a File as base64 (no data: prefix) for the JSON upload API. */
export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      resolve(result.includes(",") ? result.split(",", 2)[1] : result);
    };
    reader.onerror = () => reject(reader.error || new Error("Could not read file"));
    reader.readAsDataURL(file);
  });
}

/** Abort `ctl` when `parent` (e.g. ctx.signal) aborts. Returns an unlink function. */
export function linkAbort(parent, ctl) {
  if (!parent) return () => {};
  if (parent.aborted) {
    ctl.abort();
    return () => {};
  }
  const fn = () => ctl.abort();
  parent.addEventListener("abort", fn, { once: true });
  return () => parent.removeEventListener("abort", fn);
}
