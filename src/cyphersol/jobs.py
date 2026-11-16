"""Background jobs and staged per-file uploads (P2 item 27: ING-14, BE-14, LLM-14).

* :class:`JobManager` runs long operations (bulk ingest, analysis warm-up) on daemon worker threads and keeps
  an in-memory record per job that the HTTP API exposes for progress polling.
* :class:`UploadStore` holds files uploaded one request at a time (so a 100-file batch is never one 150 MB
  request) until an ingest job consumes them. Staged files live in memory only, expire after ``ttl_s`` and are
  bounded in total size. Passwords are never staged: they travel with the job request.

Both are process-local: a restart forgets queued jobs and staged files (the vault is unaffected).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from cyphersol.models import ConflictError

_LOG = logging.getLogger("cyphersol.jobs")

JOB_STATUSES = ("queued", "running", "succeeded", "failed")
# Share of the progress bar given to each ingest phase (they report done/total within the phase).
INGEST_PHASE_WEIGHTS: Tuple[Tuple[str, float], ...] = (
    ("parse", 0.55), ("validate", 0.25), ("entities", 0.12), ("commit", 0.08),
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class JobError(Exception):
    """A job failed in a way the API reports to the client (status code + message)."""


class Job:
    __slots__ = (
        "job_id", "kind", "case_id", "status", "phase", "done", "total", "progress", "created_at", "started_at",
        "finished_at", "result", "error", "error_type", "http_status", "error_id", "meta", "_t0", "_t1",
        "_weights",
    )

    def __init__(self, kind: str, case_id: Optional[str], meta: Optional[Dict[str, Any]] = None,
                 weights: Optional[Tuple[Tuple[str, float], ...]] = None) -> None:
        self.job_id = uuid.uuid4().hex
        self.kind = kind
        self.case_id = case_id
        self.status = "queued"
        self.phase: Optional[str] = None
        self.done = 0
        self.total = 0
        self.progress = 0.0
        self.created_at = _now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.result: Any = None
        self.error: Optional[str] = None
        self.error_type: Optional[str] = None
        self.http_status: Optional[int] = None
        self.error_id: Optional[str] = None
        self.meta: Dict[str, Any] = dict(meta or {})
        self._t0: Optional[float] = None
        self._t1: Optional[float] = None
        self._weights = weights

    def set_progress(self, phase: str, done: int, total: int) -> None:
        self.phase = phase
        self.done = int(done)
        self.total = int(total)
        frac = (done / total) if total else 1.0
        frac = min(1.0, max(0.0, frac))
        if self._weights:
            base = 0.0
            for name, w in self._weights:
                if name == phase:
                    self.progress = round(max(self.progress, base + w * frac), 4)
                    return
                base += w
        self.progress = round(max(self.progress, frac), 4)

    def to_dict(self, include_result: bool = True) -> Dict[str, Any]:
        elapsed = None
        if self._t0 is not None:
            elapsed = round((self._t1 if self._t1 is not None else time.monotonic()) - self._t0, 3)
        out: Dict[str, Any] = {
            "job_id": self.job_id,
            "kind": self.kind,
            "case_id": self.case_id,
            "status": self.status,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "progress": self.progress,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": elapsed,
            "error": self.error,
            "error_type": self.error_type,
            "http_status": self.http_status,
            **({"error_id": self.error_id} if self.error_id else {}),
            **self.meta,
        }
        if include_result:
            out["result"] = self.result
        return out


class JobManager:
    """FIFO job queue served by ``workers`` daemon threads. Finished jobs are kept (newest ``keep``)."""

    def __init__(self, workers: int = 2, keep: int = 200) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Tuple[Job, Callable[[Callable[[str, int, int], None]], Any]]]" = queue.Queue()
        self._keep = int(keep)
        self._threads: List[threading.Thread] = []
        self._n_workers = max(1, int(workers))

    def _ensure_workers(self) -> None:
        if self._threads:
            return
        for i in range(self._n_workers):
            t = threading.Thread(target=self._worker, name=f"cyphersol-job-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(
        self,
        kind: str,
        case_id: Optional[str],
        fn: Callable[[Callable[[str, int, int], None]], Any],
        meta: Optional[Dict[str, Any]] = None,
        weights: Optional[Tuple[Tuple[str, float], ...]] = None,
    ) -> Job:
        job = Job(kind, case_id, meta=meta, weights=weights)
        with self._lock:
            self._jobs[job.job_id] = job
            self._trim()
            self._ensure_workers()
        self._queue.put((job, fn))
        return job

    def _trim(self) -> None:
        finished = [jid for jid, j in self._jobs.items() if j.status in ("succeeded", "failed")]
        while len(self._jobs) > self._keep and finished:
            self._jobs.pop(finished.pop(0), None)

    def _worker(self) -> None:
        while True:
            job, fn = self._queue.get()
            try:
                self._run(job, fn)
            finally:
                self._queue.task_done()

    def _run(self, job: Job, fn: Callable[[Callable[[str, int, int], None]], Any]) -> None:
        job.status = "running"
        job.started_at = _now()
        job._t0 = time.monotonic()
        try:
            result = fn(job.set_progress)
            job.result = result
            job.progress = 1.0
            job.status = "succeeded"
            job.http_status = 200
        except ConflictError as exc:
            job.status, job.error, job.error_type, job.http_status = "failed", str(exc), "ConflictError", 409
        except KeyError as exc:
            msg = str(exc.args[0]) if exc.args else "Not found"
            job.status, job.error, job.error_type, job.http_status = "failed", msg, "NotFound", 404
        except (ValueError, JobError) as exc:
            job.status, job.error, job.error_type, job.http_status = "failed", str(exc), exc.__class__.__name__, 400
        except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised into the worker loop
            job.error_id = uuid.uuid4().hex[:12]
            _LOG.error("Job %s (%s) failed [%s]", job.job_id, job.kind, job.error_id, exc_info=True)
            job.status, job.error, job.error_type, job.http_status = (
                "failed", "Internal error while running the job", exc.__class__.__name__, 500,
            )
        finally:
            job.finished_at = _now()
            job._t1 = time.monotonic()

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(str(job_id))
        if job is None:
            raise KeyError(f"Job '{job_id}' not found")
        return job

    def list(self, case_id: Optional[str] = None, kind: Optional[str] = None) -> List[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j for j in reversed(jobs) if (case_id is None or j.case_id == case_id) and (kind is None or j.kind == kind)]

    def active(self, case_id: str, kind: str) -> Optional[Job]:
        with self._lock:
            for j in self._jobs.values():
                if j.case_id == case_id and j.kind == kind and j.status in ("queued", "running"):
                    return j
        return None

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Job:
        """Block until the job finishes (tests and the CLI)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        job = self.get(job_id)
        while job.status in ("queued", "running"):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"Job '{job_id}' still {job.status}")
            time.sleep(0.02)
        return job


class StagedUpload:
    __slots__ = ("upload_id", "case_id", "filename", "data", "sha256", "size", "created", "expires")

    def __init__(self, case_id: str, filename: str, data: bytes, ttl_s: float) -> None:
        self.upload_id = uuid.uuid4().hex
        self.case_id = case_id
        self.filename = filename
        self.data = data
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.size = len(data)
        self.created = time.monotonic()
        self.expires = self.created + ttl_s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "case_id": self.case_id,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "expires_in_s": max(0, int(self.expires - time.monotonic())),
        }


class UploadStore:
    """In-memory staging area for per-file uploads, bounded by total bytes and file count, with a TTL."""

    def __init__(self, max_total_bytes: int = 1024 * 1024 * 1024, max_files: int = 1000, ttl_s: float = 3600.0) -> None:
        self.max_total_bytes = int(max_total_bytes)
        self.max_files = int(max_files)
        self.ttl_s = float(ttl_s)
        self._items: "OrderedDict[str, StagedUpload]" = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self) -> None:
        now = time.monotonic()
        for uid in [u for u, it in self._items.items() if it.expires <= now]:
            self._items.pop(uid, None)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(it.size for it in self._items.values())

    def put(self, case_id: str, filename: str, data: bytes) -> StagedUpload:
        name = str(filename or "").strip()
        if not name:
            raise ValueError("filename is required")
        if not data:
            raise ValueError("The uploaded file is empty")
        with self._lock:
            self._expire()
            if len(self._items) >= self.max_files:
                raise ValueError(f"Too many staged uploads (limit {self.max_files}); start an ingest job first")
            used = sum(it.size for it in self._items.values())
            if used + len(data) > self.max_total_bytes:
                raise ValueError(
                    f"Staging area full ({used} of {self.max_total_bytes} bytes used); start an ingest job or "
                    "discard staged files first"
                )
            item = StagedUpload(case_id, name[:255], bytes(data), self.ttl_s)
            self._items[item.upload_id] = item
            return item

    def take(self, case_id: str, upload_ids: List[str]) -> List[StagedUpload]:
        """Remove and return the staged files (all or nothing). Unknown / expired / other-case ids -> KeyError."""
        with self._lock:
            self._expire()
            missing = [u for u in upload_ids if u not in self._items or self._items[u].case_id != case_id]
            if missing:
                raise KeyError(f"Staged upload '{missing[0]}' not found (expired, already used or another case)")
            if len(set(upload_ids)) != len(upload_ids):
                raise ValueError("upload_ids contains duplicates")
            return [self._items.pop(u) for u in upload_ids]

    def discard(self, case_id: str, upload_id: str) -> bool:
        with self._lock:
            it = self._items.get(upload_id)
            if it is None or it.case_id != case_id:
                raise KeyError(f"Staged upload '{upload_id}' not found")
            del self._items[upload_id]
            return True

    def list(self, case_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self._expire()
            return [it.to_dict() for it in self._items.values() if it.case_id == case_id]
