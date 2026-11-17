"""Local HTTP server exposing the CypherSOL REST API and Web UI.

Request hardening (BE-01 / LLM-04 / LLM-05 / EDGE-01 / UI-L-20):
  * the Host header must name a loopback host (or one listed in CYPHERSOL_ALLOWED_HOSTS) on the bound port,
    which defeats DNS rebinding;
  * an Origin header, when present, must be the same loopback origin;
  * every /api/* request except ``GET /api/health`` must carry the per-launch token in ``X-CypherSOL-Token``;
    the token is injected into index.html at serve time and is never accepted from the query string;
  * POST bodies must be ``application/json`` (415 otherwise), so cross-site "simple" requests cannot reach the API;
  * no CORS headers are emitted;
  * Tally pushes are limited to loopback hosts or the CYPHERSOL_TALLY_HOSTS allow-list.
"""

from __future__ import annotations

import base64
import fnmatch
import hmac
import html
import ipaddress
import json
import math
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, FrozenSet, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from cyphersol.ingestion.pdf_parser import PageRenderError
from cyphersol.models import ConflictError
from cyphersol.service import CypherApplicationService

logger = logging.getLogger("cyphersol.server")
MAX_REQUEST_BODY_BYTES = 150 * 1024 * 1024

TOKEN_HEADER = "X-CypherSOL-Token"
TOKEN_PLACEHOLDER = "__CYPHERSOL_TOKEN__"
ALLOWED_HOSTS_ENV = "CYPHERSOL_ALLOWED_HOSTS"
TALLY_HOSTS_ENV = "CYPHERSOL_TALLY_HOSTS"
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_NOT_FOUND_RE = re.compile(r"\bnot found\b", re.IGNORECASE)
# Case POST routes that do not change case data (or schedule their own warm-up).
_NO_WARMUP_POSTS = frozenset({"pdf-preview", "ingest-jobs", "edge/tally-push", "inputs"})

# P3 UI: explicit static MIME types (mimetypes can be wrong or missing on some hosts, and native ES modules
# are refused unless served with a JavaScript MIME type).
STATIC_MIME_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}

# Strict CSP for the UI shell (UI-S-16 / item 40): no inline script, no inline style attributes, no framing.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'"
)


MAX_QUERY_INT = 10**12  # well inside SQLite's 64-bit INTEGER range


def _reject_json_constant(name: str) -> Any:
    raise BadRequest(f"Invalid JSON request body: {name} is not allowed")


def _finite_json(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(v) for v in value]
    return value


class HttpError(Exception):
    """Raised inside a handler to send a specific status code with a safe message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class BadRequest(ValueError):
    """Malformed request input (mapped to 400)."""


def _env_host_set(env_name: str) -> FrozenSet[str]:
    raw = os.environ.get(env_name, "")
    return frozenset(h.strip().lower().strip("[]") for h in raw.split(",") if h.strip())


def is_loopback_host(host: str) -> bool:
    """True for 'localhost' or any loopback IP literal (127.0.0.0/8, ::1). Brackets around IPv6 are allowed."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def tally_host_allowed(host: str) -> bool:
    """Tally XML pushes may target loopback, or a host explicitly listed in CYPHERSOL_TALLY_HOSTS."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h:
        return False
    if is_loopback_host(h):
        return True
    return h in _env_host_set(TALLY_HOSTS_ENV)


def _split_host_port(value: str) -> Tuple[str, Optional[int]]:
    """Split an HTTP Host header value into (lowercase host without brackets, port or None)."""
    v = (value or "").strip().lower()
    if not v:
        raise ValueError("empty host")
    if v.startswith("["):
        end = v.find("]")
        if end == -1:
            raise ValueError("bad IPv6 host")
        host, rest = v[1:end], v[end + 1 :]
        if rest and not rest.startswith(":"):
            raise ValueError("bad host")
        port_s = rest[1:] if rest else ""
    else:
        if v.count(":") > 1:
            raise ValueError("unbracketed IPv6 host")
        host, _, port_s = v.partition(":")
    if port_s:
        if not port_s.isdigit():
            raise ValueError("bad port")
        port = int(port_s)
        if not 0 < port < 65536:
            raise ValueError("bad port")
        return host, port
    return host, None


def _json_body_field(body: Dict[str, Any], key: str) -> Any:
    if key not in body or body[key] is None:
        raise BadRequest(f"Missing required field '{key}'")
    return body[key]


def _b64_field(body: Dict[str, Any], key: str = "content_b64") -> bytes:
    raw = body.get(key)
    if not isinstance(raw, str) or not raw:
        raise BadRequest(f"Missing required field '{key}'")
    try:
        return base64.b64decode(raw, validate=False)
    except (ValueError, TypeError) as exc:
        raise BadRequest(f"'{key}' is not valid base64") from exc


def _opt_str(body: Dict[str, Any], key: str) -> Optional[str]:
    v = body.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise BadRequest(f"'{key}' must be a string")
    return v


def _opt_bool(body: Dict[str, Any], key: str) -> Optional[bool]:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise BadRequest(f"'{key}' must be true or false")
    return v


# PDF-21: Draw Tool page-render failures -> HTTP status (the password is never logged or echoed).
PAGE_RENDER_STATUS: Dict[str, int] = {
    "renderer_unavailable": 503,
    "password_required": 401,
    "wrong_password": 401,
    "page_out_of_range": 400,
    "corrupt": 400,
    "render_failed": 500,
}


def create_http_handler(
    service: CypherApplicationService,
    ui_dir: str,
    api_token: Optional[str] = None,
    allowed_hosts: Optional[FrozenSet[str]] = None,
):
    """
    Build the request handler class.

    ``api_token`` defaults to a fresh ``secrets.token_urlsafe(32)`` per call (i.e. per launch); it is exposed as
    ``handler_cls.api_token``. ``allowed_hosts`` adds non-loopback Host names (defaults to CYPHERSOL_ALLOWED_HOSTS).
    """
    token = api_token or secrets.token_urlsafe(32)
    ui_root = os.path.abspath(ui_dir)
    extra_hosts = frozenset(h.lower().strip("[]") for h in (allowed_hosts if allowed_hosts is not None else _env_host_set(ALLOWED_HOSTS_ENV)))
    reject_audit_lock = threading.Lock()
    reject_audit_last: Dict[str, float] = {}

    class CypherRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "CypherSOL"
        sys_version = ""
        api_token = token

        def log_message(self, format: str, *args: Any) -> None:
            # Quiet clean stdout logging
            pass

        # Set once the request body has been fully consumed; any error sent while a body may still be unread
        # closes the connection so the unread bytes can never be parsed as a pipelined request (P0 leftover).
        _body_consumed = False

        def _request_may_have_body(self) -> bool:
            if self.headers.get("Transfer-Encoding"):
                return True
            try:
                return int(self.headers.get("Content-Length", "0") or 0) != 0
            except ValueError:
                return True

        def _reject_transfer_encoding(self) -> None:
            if self.headers.get("Transfer-Encoding") is not None:
                raise HttpError(411, "Length Required: chunked or encoded request bodies are not accepted")

        # ------------------------------------------------------------------
        # Request validation
        # ------------------------------------------------------------------
        def _bound_port(self) -> Optional[int]:
            addr = getattr(self.server, "server_address", None)
            if isinstance(addr, tuple) and len(addr) >= 2 and isinstance(addr[1], int) and addr[1] > 0:
                return addr[1]
            return None

        def _host_ok(self, host: str) -> bool:
            # Exact names: "127.0.0.1", "localhost", "::1", wildcard "*", or explicit/pattern allow-list.
            if host in _LOOPBACK_NAMES or "*" in extra_hosts:
                return True
            return any(fnmatch.fnmatch(host, pat) for pat in extra_hosts)

        def _reject(self, status: int, reason: str, message: str) -> None:
            logger.warning(
                "Rejected %s %s from %s: %s (Host=%r Origin=%r)",
                self.command, urlparse(self.path).path, self.client_address[0] if self.client_address else "?",
                reason, self.headers.get("Host"), self.headers.get("Origin"),
            )
            now = time.monotonic()
            with reject_audit_lock:
                last = reject_audit_last.get(reason, 0.0)
                should_audit = now - last >= 60.0
                if should_audit:
                    reject_audit_last[reason] = now
            if should_audit:
                try:
                    service.vault.log_audit(
                        None,
                        "API_REQUEST_REJECTED",
                        {
                            "reason": reason,
                            "method": self.command,
                            "path": urlparse(self.path).path[:200],
                            "host": (self.headers.get("Host") or "")[:200],
                            "origin": (self.headers.get("Origin") or "")[:200],
                        },
                    )
                except Exception:
                    logger.debug("Could not audit rejected request", exc_info=True)
            raise HttpError(status, message)

        def _validate_request(self, path: str) -> None:
            """Host / Origin / Sec-Fetch-Site / token checks. Raises HttpError on rejection."""
            bound_port = self._bound_port()
            try:
                host, port = _split_host_port(self.headers.get("Host", ""))
            except ValueError:
                self._reject(403, "bad_host", "Forbidden: invalid Host header")
            if not self._host_ok(host):
                self._reject(403, "host_not_allowed", "Forbidden: Host not allowed")

            # Check if running behind a reverse proxy / cloud load balancer (Render, Cloud Run, AWS, Azure, Docker)
            forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            forwarded_port_raw = (self.headers.get("X-Forwarded-Port") or "").strip()
            is_forwarded = bool(forwarded_proto or forwarded_port_raw or self.headers.get("X-Forwarded-For"))
            is_cloud = is_forwarded or "*" in extra_hosts or (host not in _LOOPBACK_NAMES and self._host_ok(host))

            if bound_port is not None and not is_cloud:
                if (port or 80) != bound_port:
                    self._reject(403, "host_port_mismatch", "Forbidden: Host port mismatch")
            expected_port = bound_port if bound_port is not None else port

            origin = self.headers.get("Origin")
            if origin is not None:
                o = urlparse(origin.strip())
                try:
                    default_scheme_port = 443 if o.scheme == "https" else 80
                    o_port = o.port or default_scheme_port
                except ValueError:
                    o_port = None
                o_host = (o.hostname or "").lower()
                expected_scheme = "https" if forwarded_proto == "https" else "http"
                if (
                    o.scheme != expected_scheme
                    or not self._host_ok(o_host)
                ):
                    self._reject(403, "origin_not_allowed", "Forbidden: cross-origin request")
                if not is_cloud and expected_port is not None and o_port != expected_port:
                    self._reject(403, "origin_not_allowed", "Forbidden: cross-origin request")

            is_api = path == "/api" or path.startswith("/api/")
            if not is_api:
                return
            if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
                self._reject(403, "cross_site_fetch", "Forbidden: cross-site request")
            if self.command == "GET" and path == "/api/health":
                return
            supplied = self.headers.get(TOKEN_HEADER, "")
            if not supplied and path.endswith("/download"):
                supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                self._reject(403, "bad_token", "Forbidden: missing or invalid API token")

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            payload: bytes,
            extra_headers: Optional[Dict[str, str]] = None,
        ) -> None:
            if (not self._body_consumed and self._request_may_have_body()) or (
                status in (400, 403, 411, 413, 415) and not self._body_consumed
            ):
                self.close_connection = True
            self._last_status = status
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, data: Any) -> None:
            try:
                raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except ValueError:
                # Never emit bare NaN/Infinity tokens (invalid JSON): send them as null and log it.
                logger.warning("Non-finite number in JSON response for %s; sent as null", urlparse(self.path).path)
                raw = json.dumps(_finite_json(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", raw)

        def _send_error_json(self, exc: BaseException) -> None:
            """Map exceptions to status codes without leaking internals (BE-15)."""
            if isinstance(exc, HttpError):
                return self._send_json(exc.status, {"error": exc.message})
            if isinstance(exc, ConflictError):
                return self._send_json(409, {"error": str(exc)})
            if isinstance(exc, PageRenderError):
                code = getattr(exc, "code", "render_failed")
                return self._send_json(PAGE_RENDER_STATUS.get(code, 500),
                                       {"error": getattr(exc, "message", None) or str(exc), "code": code})
            if isinstance(exc, KeyError):
                msg = str(exc.args[0]) if exc.args else ""
                if _NOT_FOUND_RE.search(msg):
                    return self._send_json(404, {"error": msg})
                # Any other KeyError is a programming error, not a missing resource.
            elif isinstance(exc, ValueError):
                return self._send_json(400, {"error": str(exc)})
            elif isinstance(exc, OverflowError):
                # A number too large for an int/SQLite column is a bad request, not an oversized body.
                return self._send_json(400, {"error": "Numeric value out of range"})
            error_id = uuid.uuid4().hex[:12]
            logger.error(
                "Unhandled error [%s] handling %s %s", error_id, self.command, urlparse(self.path).path,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return self._send_json(500, {"error": "Internal server error", "error_id": error_id})

        def _read_json_body(self) -> Dict[str, Any]:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise HttpError(415, "Unsupported Media Type: Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError as exc:
                raise BadRequest("Invalid Content-Length header") from exc
            if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                raise HttpError(
                    413, f"Request body exceeds maximum allowed size of {MAX_REQUEST_BODY_BYTES} bytes"
                )
            raw = self.rfile.read(length) if length > 0 else b"{}"
            self._body_consumed = True
            try:
                body = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BadRequest("Invalid JSON request body") from exc
            if not isinstance(body, dict):
                raise BadRequest("JSON request body must be an object")
            return body

        def _serve_static(self, path: str) -> None:
            rel_path = "index.html" if path in ("/", "") else path.lstrip("/")
            safe_path = os.path.normpath(os.path.join(ui_root, rel_path))
            if os.path.commonpath([ui_root, safe_path]) != ui_root or not os.path.isfile(safe_path):
                return self._send_json(404, {"error": "Not found"})
            ext = os.path.splitext(safe_path)[1].lower()
            ctype = STATIC_MIME_TYPES.get(ext) or mimetypes.guess_type(safe_path)[0]
            with open(safe_path, "rb") as f:
                payload = f.read()
            extra: Dict[str, str] = {}
            if os.path.basename(safe_path) == "index.html":
                payload = payload.replace(
                    TOKEN_PLACEHOLDER.encode("ascii"), html.escape(token, quote=True).encode("ascii")
                )
                ctype = "text/html; charset=utf-8"
            if ctype and ctype.startswith("text/html"):
                extra["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            return self._send_bytes(200, ctype or "application/octet-stream", payload, extra)

        def do_OPTIONS(self) -> None:
            self._body_consumed = False
            # No CORS: the UI is same-origin, so preflights from other origins must fail.
            try:
                self._reject_transfer_encoding()
                self._validate_request(urlparse(self.path).path)
            except HttpError as exc:
                return self._send_json(exc.status, {"error": exc.message})
            if self._request_may_have_body():
                self.close_connection = True
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Allow", "GET, POST, PUT, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._body_consumed = False
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            def qstr(key: str, default: Optional[str] = None) -> Optional[str]:
                vals = qs.get(key)
                return vals[0] if vals and vals[0] != "" else default

            def qfloat(key: str, default: float) -> float:
                v = qstr(key)
                if v is None:
                    return default
                try:
                    val = float(v)
                except ValueError as exc:
                    raise ValueError(f"Invalid numeric query parameter '{key}': {v!r}") from exc
                if not math.isfinite(val):
                    raise ValueError(f"Invalid numeric query parameter '{key}': must be a finite number")
                return val

            def qbool(key: str) -> Optional[bool]:
                v = qstr(key)
                if v is None:
                    return None
                lv = v.strip().lower()
                if lv in ("1", "true", "yes"):
                    return True
                if lv in ("0", "false", "no"):
                    return False
                raise ValueError(f"Invalid boolean query parameter '{key}': {v!r}")

            def qint(key: str, default: int) -> int:
                v = qstr(key)
                if v is None:
                    return default
                try:
                    val = int(v)
                except ValueError as exc:
                    raise ValueError(f"Invalid numeric query parameter '{key}': {v!r}") from exc
                if abs(val) > MAX_QUERY_INT:
                    raise ValueError(f"Invalid numeric query parameter '{key}': out of range")
                return val

            def qfloat_opt(key: str) -> Optional[float]:
                if qstr(key) is None:
                    return None
                val = qfloat(key, 0.0)
                if val != val or val in (float("inf"), float("-inf")):
                    raise ValueError(f"Invalid numeric query parameter '{key}'")
                return val

            def qint_opt(key: str) -> Optional[int]:
                return qint(key, 0) if qstr(key) is not None else None

            def send_download(body: bytes, ctype: str, filename: str) -> None:
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:150] or "download"
                extra = {"Content-Disposition": f'attachment; filename="{safe}"'}
                if ctype.startswith("text/html"):
                    # Generated reports embed statement text: never let them run script on this origin.
                    extra["Content-Security-Policy"] = (
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
                    )
                return self._send_bytes(200, ctype, body, extra)

            try:
                self._reject_transfer_encoding()
                self._validate_request(path)
                if path == "/api/health":
                    meta = service.get_metadata()
                    cfg = service.llm_config
                    egress = bool(cfg.cloud_egress_possible())
                    return self._send_json(200, {
                        "status": "ok",
                        "engine": "CypherSOL Offline Core",
                        "version": meta["version"],
                        # Honest offline status (LLM-18): false whenever a cloud LLM key is usable.
                        "offline": not egress,
                        "cloud_egress_possible": egress,
                        "execution_mode": cfg.execution_mode,
                        "effective_execution_mode": cfg.effective_execution_mode(),
                        "llm_provider": cfg.provider if egress else None,
                        "llm_key_source": cfg.key_source() if egress else "none",
                        "banks_supported": meta["banks_supported_count"],
                        "categories": meta["categories_count"],
                        "knowledge_base_points": meta["knowledge_base_data_points"],
                    })

                if path == "/api/meta":
                    return self._send_json(200, service.get_metadata())

                if path == "/api/llm/config":
                    return self._send_json(200, service.get_llm_config_public())

                if path == "/api/cases":
                    return self._send_json(200, {"cases": service.list_cases()})

                job_m = re.match(r"^/api/jobs/([0-9a-f]{32})$", path)
                if job_m:
                    inc = qbool("include_result")
                    return self._send_json(200, service.get_job(job_m.group(1), include_result=inc is not False))

                # Case-scoped GET routes
                m = re.match(r"^/api/cases/([^/]+)/(.+)$", path)
                if m:
                    cid, sub = m.group(1), m.group(2)
                    if sub == "jobs":
                        return self._send_json(200, service.list_jobs(cid))
                    if sub == "uploads":
                        return self._send_json(200, service.list_staged_uploads(cid))
                        return self._send_json(200, service.list_staged_uploads(cid))
                    if sub == "overview":
                        return self._send_json(
                            200,
                            service.get_case_overview(
                                cid, tx_offset=qint("tx_offset", 0), tx_limit=qint("tx_limit", 0)
                            ),
                        )
                    if sub == "transactions":
                        cat = qstr("category_id")
                        return self._send_json(
                            200,
                            service.list_transactions(
                                cid,
                                offset=qint("offset", 0),
                                limit=qint("limit", 200),
                                q=qstr("q"),
                                category_id=int(cat) if cat is not None else None,
                                is_suspense=qbool("suspense"),
                                flagged=qbool("flagged"),
                                include_ignored=qbool("include_ignored"),
                                statement_id=qstr("statement_id"),
                                entity_id=qstr("entity_id"),
                                channel=qstr("channel"),
                                direction=qstr("direction"),
                                date_from=qstr("date_from"),
                                date_to=qstr("date_to"),
                                sort=qstr("sort"),
                                order=qstr("order"),
                                amount_min=qfloat_opt("amount_min"),
                                amount_max=qfloat_opt("amount_max"),
                                tx_ids=qstr("tx_ids"),
                            ),
                        )
                    tx_prev = re.match(r"^transactions/([^/]+)/resolve-preview$", sub)
                    if tx_prev:
                        return self._send_json(200, service.preview_resolution(cid, tx_prev.group(1)))
                    if sub == "provenance":
                        return self._send_json(200, service.get_statement_provenance(cid))
                    if sub == "edge/tally":
                        return self._send_json(
                            200,
                            service.get_tally_preview(
                                cid,
                                include_suspense=bool(qbool("include_suspense")),
                                company_name=qstr("company_name"),
                                use_trade_vouchers=qbool("use_trade_vouchers"),
                                include_xml=bool(qbool("include_xml")),
                            ),
                        )
                    if sub == "edge/tally-map":
                        return self._send_json(200, service.get_tally_ledger_map(cid))
                    if sub == "edge/itr":
                        return self._send_json(200, service.get_itr_report(cid, fy=qstr("fy")))
                    if sub == "edge/loan":
                        foir = qfloat("foir", 55.0)
                        rate = qfloat("rate", 11.5)
                        tenure = qint("tenure", 60)
                        if not 0 < foir <= 100:
                            raise BadRequest("foir must be a percentage greater than 0 and at most 100")
                        if not 0 <= rate <= 50:
                            raise BadRequest("rate must be an annual percentage between 0 and 50")
                        if not 1 <= tenure <= 480:
                            raise BadRequest("tenure must be a whole number of months between 1 and 480")
                        inc_f = qfloat("income_factor", 0.20)
                        abb_f = qfloat("abb_factor", 0.50)
                        override = qfloat_opt("income_override")
                        if not 0 <= inc_f <= 1:
                            raise BadRequest("income_factor must be between 0 and 1")
                        if not 0 <= abb_f <= 1:
                            raise BadRequest("abb_factor must be between 0 and 1")
                        if override is not None and override < 0:
                            raise BadRequest("income_override must be zero or greater")
                        return self._send_json(
                            200,
                            service.get_loan_report(
                                cid, foir_pct=foir, annual_rate=rate, tenure_months=tenure,
                                business_income_factor=inc_f, abb_income_factor=abb_f,
                                monthly_income_override=override,
                            ),
                        )
                    if sub == "trace/mules":
                        return self._send_json(200, {"assessments": service.get_mule_assessments(cid)})
                    if sub == "trace/graph":
                        return self._send_json(
                            200,
                            service.get_network_graph(
                                cid,
                                date_from=qstr("date_from"),
                                date_to=qstr("date_to"),
                                min_amount=qfloat("min_amount", 0.0),
                                channel=qstr("channel"),
                                keyword=qstr("keyword"),
                                max_amount=qfloat_opt("max_amount"),
                                ifsc=qstr("ifsc"),
                                max_nodes=qint_opt("max_nodes"),
                                layout=qstr("layout"),
                            ),
                        )
                    if sub == "trace/graph/expand":
                        return self._send_json(
                            200,
                            service.expand_network_node(
                                cid,
                                entity_id=qstr("entity_id") or "",
                                limit=qint("limit", 50),
                                date_from=qstr("date_from"),
                                date_to=qstr("date_to"),
                                min_amount=qfloat("min_amount", 0.0),
                                channel=qstr("channel"),
                                keyword=qstr("keyword"),
                                max_amount=qfloat_opt("max_amount"),
                                ifsc=qstr("ifsc"),
                            ),
                        )
                    if sub == "trace/bidirectional":
                        return self._send_json(
                            200,
                            service.get_bidirectional(
                                cid,
                                entity_a_id=qstr("a"),
                                entity_b_id=qstr("b"),
                                rapid_window_hours=qfloat("rapid_window_hours", 48.0),
                                round_trip_window_hours=qfloat("round_trip_window_hours", 720.0),
                                amount_tolerance=qfloat("amount_tolerance", 0.10),
                                date_from=qstr("date_from"),
                                date_to=qstr("date_to"),
                            ),
                        )
                    if sub == "trace/layering":
                        return self._send_json(
                            200,
                            service.get_layering(
                                cid,
                                source_entity_id=qstr("source_entity_id"),
                                window_days=qint("window_days", 14),
                                max_depth=qint("max_depth", 4),
                                mode=qstr("mode", "fifo"),
                                root_tx_id=qstr("root_tx_id"),
                                date_from=qstr("date_from"),
                                date_to=qstr("date_to"),
                                trace_depth=qint_opt("trace_depth"),
                            ),
                        )
                    if sub == "trace/layering/expand":
                        return self._send_json(
                            200,
                            service.expand_layering(
                                cid,
                                source_entity_id=qstr("source") or qstr("source_entity_id"),
                                node_path=qstr("path"),
                                window_days=qint("window_days", 14),
                                mode=qstr("mode", "fifo"),
                                root_tx_id=qstr("root_tx_id"),
                                date_from=qstr("date_from"),
                                date_to=qstr("date_to"),
                                trace_depth=qint_opt("trace_depth"),
                            ),
                        )
                    if sub == "trace/loops":
                        mcl = qstr("max_cycle_length")
                        return self._send_json(
                            200,
                            service.get_loops_report(
                                cid,
                                max_days=qint("max_days", 30),
                                min_retention=qfloat("min_retention", 0.50),
                                max_cycle_length=qint("max_cycle_length", 6) if mcl is not None else None,
                                max_instances_per_loop=qint_opt("max_instances_per_loop"),
                                hub_degree_cap=qint_opt("hub_degree_cap"),
                            ),
                        )
                    if sub == "trace/profile":
                        return self._send_json(200, service.get_financial_profile(cid, entity_id=qstr("entity_id")))
                    if sub == "export/xlsx":
                        xlsx_bytes = service.export_xlsx(cid)
                        return self._send_bytes(
                            200,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            xlsx_bytes,
                            {"Content-Disposition": f'attachment; filename="{cid}_audit_report.xlsx"'},
                        )
                    if sub == "entities/preview":
                        thr = qstr("threshold")
                        return self._send_json(
                            200, service.preview_entity_clusters(cid, qfloat("threshold", 0.0) if thr is not None else None)
                        )
                    if sub == "entities/decisions":
                        return self._send_json(200, service.get_entity_decisions(cid))
                    if sub == "export/profile":
                        body_b, ctype, fname = service.export_profile(
                            cid, qstr("entity_id"), qstr("format") or qstr("fmt") or "html"
                        )
                        return send_download(body_b, ctype, fname)
                    if sub == "export/tally-xlsx":
                        return send_download(
                            service.export_tally_xlsx(
                                cid,
                                include_suspense=bool(qbool("include_suspense")),
                                company_name=qstr("company_name"),
                                use_trade_vouchers=qbool("trade_vouchers") if qstr("trade_vouchers") is not None
                                else qbool("use_trade_vouchers"),
                            ),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            f"{cid}_tally_import.xlsx",
                        )
                    if sub == "export/itr-xlsx":
                        fy = qstr("fy")
                        return send_download(
                            service.export_itr_xlsx(cid, fy=fy),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            f"{cid}_itr_{fy or 'latest'}.xlsx",
                        )
                    if sub == "export/tally-xml":
                        tally = service.export_tally_xml(
                            cid,
                            include_suspense=bool(qbool("include_suspense")),
                            company_name=qstr("company_name"),
                            use_trade_vouchers=qbool("use_trade_vouchers"),
                        )
                        return self._send_bytes(
                            200,
                            "application/xml; charset=utf-8",
                            tally["xml"].encode("utf-8"),
                            {"Content-Disposition": f'attachment; filename="{cid}_tally_import.xml"'},
                        )
                    if sub == "export/dossier-html":
                        html_doc = service.export_dossier_html(cid)
                        return self._send_bytes(200, "text/html; charset=utf-8", html_doc.encode("utf-8"))
                    if sub == "export/dossier-pdf":
                        pdf_bytes = service.export_dossier_pdf(cid)
                        return self._send_bytes(
                            200,
                            "application/pdf",
                            pdf_bytes,
                            {"Content-Disposition": f'attachment; filename="{cid}_forensic_dossier.pdf"'},
                        )
                    if sub == "inputs":
                        return self._send_json(200, {"inputs": service.list_case_inputs(cid)})
                    if sub.startswith("inputs/") and sub.endswith("/download"):
                        ident = sub[len("inputs/") : -len("/download")]
                        res = service.get_case_input_file(cid, ident)
                        if not res:
                            return self._send_json(404, {"error": "Input file not found"})
                        fname, ctype, b = res
                        return send_download(b, ctype, fname)
                    if sub == "outputs":
                        return self._send_json(200, {"outputs": service.list_case_outputs(cid)})
                    if sub.startswith("outputs/") and sub.endswith("/download"):
                        ident = sub[len("outputs/") : -len("/download")]
                        res = service.get_case_output_file(cid, ident)
                        if not res:
                            return self._send_json(404, {"error": "Output file not found"})
                        fname, ctype, b = res
                        return send_download(b, ctype, fname)

                if path.startswith("/api/"):
                    return self._send_json(404, {"error": "Unknown endpoint"})
                # Serve static UI files
                return self._serve_static(path)

            except Exception as exc:
                return self._send_error_json(exc)

        def do_PUT(self) -> None:
            # PUT exists only for the per-case Tally ledger map (same checks as POST: token, JSON body).
            return self.do_POST()

        def do_POST(self) -> None:
            self._last_status = None
            self._do_post()
            # Keep the analysis cache warm after a successful case mutation (P2: warm cache, BE-07). Read-only
            # POSTs and the ingest job (which schedules its own warm-up) are skipped.
            m = re.match(r"^/api/cases/([^/]+)/(.+)$", urlparse(self.path).path)
            if m and self._last_status is not None and 200 <= self._last_status < 300 and m.group(2) not in _NO_WARMUP_POSTS \
                    and not m.group(2).startswith("uploads"):
                try:
                    service.schedule_warmup(m.group(1))
                except Exception:  # noqa: BLE001 - warm-up is best effort
                    logger.debug("warm-up scheduling failed", exc_info=True)

        _last_status: Optional[int] = None

        def _do_post(self) -> None:
            self._body_consumed = False
            parsed = urlparse(self.path)
            path = parsed.path

            try:
                self._reject_transfer_encoding()
                self._validate_request(path)
                if self.command == "PUT" and not re.match(r"^/api/cases/[^/]+/edge/tally-map$", path):
                    raise HttpError(405, "Method Not Allowed")
                body = self._read_json_body()

                if path == "/api/draw/page-image":
                    hints = body.get("password_hints")
                    if hints is not None and not isinstance(hints, dict):
                        raise BadRequest("'password_hints' must be an object")
                    res = service.render_draw_page(
                        _b64_field(body),
                        page=body.get("page", 1),
                        dpi=body.get("dpi", 110),
                        password=_opt_str(body, "password"),
                        hints=hints,
                    )
                    png = res.pop("png")
                    res["png_b64"] = base64.b64encode(png).decode("ascii")
                    res["data_url"] = "data:image/png;base64," + res["png_b64"]
                    return self._send_json(200, res)

                if path == "/api/pdf/password-check":
                    hints = body.get("password_hints")
                    if hints is not None and not isinstance(hints, dict):
                        raise BadRequest("'password_hints' must be an object")
                    return self._send_json(
                        200,
                        service.check_pdf_password(_b64_field(body), password=_opt_str(body, "password"), hints=hints),
                    )

                if path == "/api/llm/config":
                    return self._send_json(200, service.update_llm_config(body))

                if path == "/api/llm/test":
                    return self._send_json(200, service.test_llm_connection(body))

                if path == "/api/cases":
                    created = service.create_case(
                        title=body.get("title", "New Workspace"),
                        mode=body.get("mode", "cypher_trace"),
                        agency_or_firm_name=body.get("agency_or_firm_name"),
                    )
                    return self._send_json(201, created)

                if path == "/api/draw-templates":
                    tpl = service.save_draw_template(
                        bank_code=body.get("bank_code", "CUSTOM"),
                        template_name=body.get("template_name", "Custom PDF Layout"),
                        table_bbox=body.get("table_bbox", [0.0, 0.0, 1.0, 1.0]),
                        column_splits=body.get("column_splits", [0.16, 0.50, 0.64, 0.75, 0.87]),
                        column_names=body.get("column_names", ["date", "narration", "ref", "debit", "credit", "balance"]),
                        case_id=body.get("case_id"),
                    )
                    return self._send_json(201, tpl)

                m = re.match(r"^/api/cases/([^/]+)/(.+)$", path)
                if m:
                    cid, sub = m.group(1), m.group(2)

                    if sub == "pdf-preview":
                        pdf_bytes = base64.b64decode(str(body.get("content_b64", "")), validate=False)
                        hints = body.get("password_hints")
                        if hints is not None and not isinstance(hints, dict):
                            raise BadRequest("'password_hints' must be an object")
                        return self._send_json(
                            200,
                            service.preview_pdf_for_draw_tool(pdf_bytes, password=body.get("password"), hints=hints),
                        )

                    if sub == "upload":
                        files_in = []
                        file_hints = []
                        files_payload = body.get("files", [])
                        if not isinstance(files_payload, list):
                            raise BadRequest("'files' must be a list")
                        for fitem in files_payload:
                            if not isinstance(fitem, dict):
                                raise BadRequest("Each file entry must be an object")
                            raw = base64.b64decode(str(_json_body_field(fitem, "content_b64")))
                            pwd = fitem.get("password")
                            if pwd is not None and not isinstance(pwd, str):
                                raise BadRequest("'password' must be a string")
                            fh = fitem.get("password_hints")
                            if fh is not None and not isinstance(fh, dict):
                                raise BadRequest("'password_hints' must be an object")
                            file_hints.append(fh)
                            files_in.append((str(_json_body_field(fitem, "filename")), raw, pwd or None))
                        batch_hints = body.get("password_hints")
                        if batch_hints is not None and not isinstance(batch_hints, dict):
                            raise BadRequest("'password_hints' must be an object")
                        res = service.ingest_statements(
                            cid,
                            files_in,
                            template_id=body.get("template_id"),
                            execution_mode=body.get("execution_mode"),
                            on_duplicate=str(body.get("on_duplicate") or "reject"),
                            password_hints=batch_hints,
                            file_password_hints=file_hints if any(file_hints) else None,
                        )
                        return self._send_json(200, res)

                    if sub == "inputs":
                        raw_fn = str(_json_body_field(body, "filename"))
                        content = _b64_field(body, "content_b64")
                        meta = body.get("metadata")
                        if meta is not None and not isinstance(meta, dict):
                            raise BadRequest("'metadata' must be an object")
                        record = service.upload_case_document(cid, raw_fn, content, metadata=meta)
                        return self._send_json(201, record)

                    if sub == "uploads":
                        # Per-file staging (P2 item 27): one file per request, consumed by an ingest job.
                        raw = base64.b64decode(str(_json_body_field(body, "content_b64")))
                        staged = service.stage_upload(cid, str(_json_body_field(body, "filename")), raw)
                        return self._send_json(201, staged)

                    up_discard = re.match(r"^uploads/([0-9a-f]{32})/discard$", sub)
                    if up_discard:
                        return self._send_json(200, service.discard_upload(cid, up_discard.group(1)))

                    if sub == "ingest-jobs":
                        specs = []
                        files_payload = body.get("files", [])
                        if not isinstance(files_payload, list) or not files_payload:
                            raise BadRequest("'files' must be a non-empty list")
                        for fitem in files_payload:
                            if not isinstance(fitem, dict):
                                raise BadRequest("Each file entry must be an object")
                            pwd = fitem.get("password")
                            if pwd is not None and not isinstance(pwd, str):
                                raise BadRequest("'password' must be a string")
                            fh = fitem.get("password_hints")
                            if fh is not None and not isinstance(fh, dict):
                                raise BadRequest("'password_hints' must be an object")
                            spec: Dict[str, Any] = {"password": pwd or None, "password_hints": fh}
                            if fitem.get("upload_id") is not None:
                                if not isinstance(fitem["upload_id"], str):
                                    raise BadRequest("'upload_id' must be a string")
                                spec["upload_id"] = fitem["upload_id"]
                            else:
                                spec["filename"] = str(_json_body_field(fitem, "filename"))
                                spec["content"] = base64.b64decode(str(_json_body_field(fitem, "content_b64")))
                            specs.append(spec)
                        batch_hints = body.get("password_hints")
                        if batch_hints is not None and not isinstance(batch_hints, dict):
                            raise BadRequest("'password_hints' must be an object")
                        job = service.start_ingest_job(
                            cid,
                            specs,
                            template_id=body.get("template_id"),
                            execution_mode=body.get("execution_mode"),
                            on_duplicate=str(body.get("on_duplicate") or "reject"),
                            password_hints=batch_hints,
                        )
                        return self._send_json(202, job)

                    if sub == "run-agents":
                        res = service.run_case_llm_agents(
                            cid,
                            execution_mode=body.get("execution_mode"),
                            stages=body.get("stages"),
                        )
                        return self._send_json(200, res)

                    tx_res = re.match(r"^transactions/([^/]+)/resolve$", sub)
                    if tx_res:
                        tx_id = tx_res.group(1)
                        confirmed = body.get("confirmed_tx_ids")
                        if confirmed is not None and not (
                            isinstance(confirmed, list) and all(isinstance(x, str) for x in confirmed)
                        ):
                            raise BadRequest("'confirmed_tx_ids' must be a list of transaction ids")
                        res = service.resolve_suspense(
                            cid,
                            tx_id=tx_id,
                            category_id=int(_json_body_field(body, "category_id")),
                            custom_ledger_name=body.get("custom_ledger_name"),
                            auto_refresh_similar=bool(body.get("auto_refresh_similar", True)),
                            confirmed_tx_ids=confirmed,
                            rule_scope=str(body.get("rule_scope") or "case"),
                            learn_single_token_party=bool(body.get("learn_single_token_party", False)),
                        )
                        return self._send_json(200, res)

                    tx_dup = re.match(r"^transactions/([^/]+)/toggle-duplicate$", sub)
                    if tx_dup:
                        return self._send_json(200, service.toggle_duplicate_ignore(cid, tx_dup.group(1)))

                    if sub == "entities/recluster":
                        manual_pair = None
                        if body.get("alias_name") and body.get("canonical_target"):
                            manual_pair = (body["alias_name"], body["canonical_target"])
                        res = service.recluster_entities(
                            cid,
                            similarity_threshold=body.get("similarity_threshold"),
                            manual_merge=manual_pair,
                        )
                        return self._send_json(200, res)

                    if sub == "entities/split":
                        aliases = _json_body_field(body, "aliases")
                        if not isinstance(aliases, list):
                            raise BadRequest("'aliases' must be a list")
                        return self._send_json(200, service.split_entity(
                            cid, str(_json_body_field(body, "entity_id")), aliases, note=body.get("note")))
                    if sub == "entities/merge":
                        return self._send_json(200, service.merge_entities(
                            cid, str(_json_body_field(body, "source_entity_id")),
                            str(_json_body_field(body, "target_entity_id")), note=body.get("note")))
                    if sub == "entities/reject":
                        return self._send_json(200, service.reject_entity_merge(
                            cid, str(_json_body_field(body, "entity_a_id")),
                            str(_json_body_field(body, "entity_b_id")), note=body.get("note")))
                    if sub == "entities/undo":
                        return self._send_json(200, service.undo_entity_decision(cid, _opt_str(body, "decision_id")))

                    if sub == "dedup":
                        return self._send_json(200, service.dedup_transactions(cid, apply=bool(_opt_bool(body, "apply"))))

                    if sub == "edge/tally-map":
                        if "ledger_map" in body:
                            data = body.get("ledger_map")
                        elif body.get("reset") is True:
                            data = None
                        else:
                            data = body
                        return self._send_json(200, service.set_tally_ledger_map(cid, data))

                    if sub == "edge/tally-push":
                        tally_host = str(body.get("host") or "127.0.0.1").strip()
                        try:
                            tally_port = int(body.get("port", 9000))
                        except (TypeError, ValueError) as exc:
                            raise BadRequest("Tally port must be an integer") from exc
                        if not 0 < tally_port < 65536:
                            raise BadRequest("Tally port must be between 1 and 65535")
                        bare_host = tally_host[1:-1] if tally_host.startswith("[") and tally_host.endswith("]") else tally_host
                        is_ip = True
                        try:
                            ip_obj = ipaddress.ip_address(bare_host)
                        except ValueError:
                            is_ip = False
                        if not is_ip and not _HOSTNAME_RE.match(bare_host):
                            raise BadRequest("Invalid Tally host")
                        if not tally_host_allowed(bare_host):
                            self._reject(
                                403,
                                "tally_host_not_allowed",
                                f"Tally push host '{bare_host}' is not allowed. Use a loopback address or add it "
                                f"to the {TALLY_HOSTS_ENV} environment variable.",
                            )
                        url_host = f"[{bare_host}]" if is_ip and ip_obj.version == 6 else bare_host
                        res = service.push_tally(
                            cid,
                            host=url_host,
                            port=tally_port,
                            include_suspense=bool(body.get("include_suspense", False)),
                            company_name=(str(body["company_name"]) if body.get("company_name") else None),
                            use_trade_vouchers=_opt_bool(body, "use_trade_vouchers"),
                        )
                        return self._send_json(200, res)

                    if sub == "trace/layering/misc":
                        path_arg = body.get("path")
                        if path_arg is None and body.get("entity_id") is None:
                            raise BadRequest("Missing required field 'entity_id' (or 'path')")
                        res = service.toggle_layering_misc(
                            cid,
                            entity_id=str(body["entity_id"]) if body.get("entity_id") is not None else None,
                            move_to_misc=bool(body.get("move_to_misc", True)),
                            path=path_arg,
                            source_entity_id=_opt_str(body, "source_entity_id"),
                            root_tx_id=_opt_str(body, "root_tx_id"),
                        )
                        return self._send_json(200, res)

                return self._send_json(404, {"error": "Unknown endpoint"})
            except Exception as exc:
                return self._send_error_json(exc)

    return CypherRequestHandler


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    db_path: str = "cyphersol_vault.db",
    sample_dir: str = "sample_statements",
    llm_transport_fn: Optional[Any] = None,
    api_token: Optional[str] = None,
    warm_analysis: Optional[bool] = None,
) -> ThreadingHTTPServer:
    """Create (not start) the HTTP server. A fresh per-launch API token is generated unless one is given.
    ``warm_analysis`` (default on; ``CYPHERSOL_WARM_ANALYSIS=0`` turns it off) precomputes the default analytics
    in the background after ingests and mutations."""
    if warm_analysis is None:
        warm_analysis = os.environ.get("CYPHERSOL_WARM_ANALYSIS", "1").strip().lower() not in ("0", "false", "no")
    service = CypherApplicationService(
        db_path=db_path,
        sample_dir=sample_dir,
        seed_demo=True,
        llm_transport_fn=llm_transport_fn,
        warm_analysis=warm_analysis,
    )
    ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
    handler_cls = create_http_handler(service, ui_dir, api_token=api_token)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.api_token = handler_cls.api_token  # type: ignore[attr-defined]
    return server
