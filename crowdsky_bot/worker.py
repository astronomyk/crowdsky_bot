"""Single-consumer job worker + live progress state (DESIGN.md §5).

One background thread runs one job at a time, so the dawn trigger and any number
of browser clicks can never overlap. The web layer reads :class:`Progress`.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import jobs

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    kind: str                       # refresh | stack | upload | purge
    triggered_by: str = "manual"    # manual | auto
    scopes: object = "all"          # "all" or list[str] of IPs
    targets: object = "all"         # "all" or list[str] of target names
    dry_run: bool = False


class Progress:
    """Thread-safe snapshot of what the worker is doing."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = "idle"
        self.kind = None
        self.started_at = None
        self.message = ""
        self.per_scope: dict[str, str] = {}
        self.last_run: dict | None = None

    def begin(self, kind: str) -> None:
        with self._lock:
            self.state = "running"
            self.kind = kind
            self.started_at = _now()
            self.message = ""
            self.per_scope = {}

    def end(self, last_run: dict) -> None:
        with self._lock:
            self.state = "idle"
            self.kind = None
            self.started_at = None
            self.message = ""
            self.per_scope = {}
            self.last_run = last_run

    def set_message(self, msg: str) -> None:
        with self._lock:
            self.message = msg
        log.info("%s", msg)

    def set_scope(self, ip: str, msg: str) -> None:
        with self._lock:
            self.per_scope[ip] = msg
        log.debug("[%s] %s", ip, msg)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "kind": self.kind,
                "started_at": self.started_at,
                "message": self.message,
                "per_scope": dict(self.per_scope),
                "last_run": self.last_run,
            }


class Worker(threading.Thread):
    def __init__(self, ctx):
        super().__init__(daemon=True, name="crowdsky-worker")
        self.ctx = ctx
        self.progress = Progress()
        self._q: "queue.Queue[Job]" = queue.Queue()
        self._stop = threading.Event()
        self._pending = Counter()      # kind -> count queued (for de-dup)
        self._pending_lock = threading.Lock()

    def enqueue(self, job: Job) -> bool:
        """Queue a job. Collapses duplicate pending ``refresh`` jobs."""
        with self._pending_lock:
            if job.kind == "refresh" and self._pending["refresh"] > 0:
                return False
            self._pending[job.kind] += 1
        self._q.put(job)
        return True

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            with self._pending_lock:
                self._pending[job.kind] -= 1
            try:
                self._run_job(job)
            finally:
                self._q.task_done()

    def _run_job(self, job: Job) -> None:
        cfg, cache = self.ctx.cfg, self.ctx.cache
        scope_ips = (self.ctx.scope_ips()
                     if job.scopes in ("all", None) else list(job.scopes))

        run_id = cache.start_run(job.kind, job.triggered_by)
        self.progress.begin(job.kind)
        status, summary = "ok", {}
        try:
            if job.kind == "refresh":
                summary = jobs.refresh(cfg, cache, self.progress, scope_ips,
                                       dry_run=job.dry_run)
            elif job.kind in ("stack", "upload", "purge"):
                fn = getattr(jobs, job.kind)
                summary = fn(cfg, cache, self.progress, scope_ips, job.targets,
                             dry_run=job.dry_run)
            else:
                raise ValueError(f"unknown job kind: {job.kind}")
        except Exception as exc:  # noqa: BLE001
            status = "error"
            summary = {"error": str(exc)}
            log.exception("job %s failed", job.kind)

        cache.finish_run(run_id, status, summary)
        self.progress.end({
            "kind": job.kind,
            "status": status,
            "triggered_by": job.triggered_by,
            "finished_at": _now(),
            "summary": summary,
        })

        # Reflect reality after any mutating job.
        if job.kind in ("stack", "upload", "purge") and not job.dry_run:
            self.enqueue(Job(kind="refresh", triggered_by="auto"))
