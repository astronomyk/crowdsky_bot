"""SQLite status cache + audit log.

One connection shared across the worker and web threads, serialised by a lock
(SQLite writes are cheap and infrequent here). WAL mode keeps reads concurrent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes (
    ip        TEXT PRIMARY KEY,
    hostname  TEXT,
    name      TEXT,
    firmware  TEXT,
    lon       REAL,
    lat       REAL,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS targets (
    scope_ip          TEXT,
    target            TEXT,
    on_server         INTEGER DEFAULT 0,
    awaiting_upload   INTEGER DEFAULT 0,
    awaiting_stacking INTEGER DEFAULT 0,
    updated_at        TEXT,
    PRIMARY KEY (scope_ip, target)
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT,
    triggered_by TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
    chunk_key   TEXT,
    object_name TEXT,
    scope_ip    TEXT,
    filename    TEXT,
    uploaded_at TEXT,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Cache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- scopes -------------------------------------------------------------
    def upsert_scope(self, ip, hostname="", name="", firmware="",
                     lon=None, lat=None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO scopes (ip, hostname, name, firmware, lon, lat,
                                       last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET
                       hostname=excluded.hostname, name=excluded.name,
                       firmware=excluded.firmware, lon=excluded.lon,
                       lat=excluded.lat, last_seen=excluded.last_seen""",
                (ip, hostname, name, firmware, lon, lat, _now()),
            )
            self._conn.commit()

    def get_scopes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM scopes ORDER BY hostname").fetchall()
        return [dict(r) for r in rows]

    # -- targets (status cache) --------------------------------------------
    def upsert_target(self, scope_ip, target, on_server, awaiting_upload,
                      awaiting_stacking) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO targets (scope_ip, target, on_server,
                        awaiting_upload, awaiting_stacking, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scope_ip, target) DO UPDATE SET
                       on_server=excluded.on_server,
                       awaiting_upload=excluded.awaiting_upload,
                       awaiting_stacking=excluded.awaiting_stacking,
                       updated_at=excluded.updated_at""",
                (scope_ip, target, on_server, awaiting_upload,
                 awaiting_stacking, _now()),
            )
            self._conn.commit()

    def prune_targets(self, scope_ip: str, keep: set[str]) -> None:
        """Drop cached target rows for a scope that are no longer present."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT target FROM targets WHERE scope_ip=?", (scope_ip,)
            ).fetchall()
            stale = [r["target"] for r in rows if r["target"] not in keep]
            for t in stale:
                self._conn.execute(
                    "DELETE FROM targets WHERE scope_ip=? AND target=?",
                    (scope_ip, t),
                )
            self._conn.commit()

    def count_targets_by_scope(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT scope_ip, COUNT(*) AS n FROM targets GROUP BY scope_ip"
            ).fetchall()
        return {r["scope_ip"]: r["n"] for r in rows}

    def get_summary(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.scope_ip, s.hostname, s.name, t.target,
                          t.on_server, t.awaiting_upload, t.awaiting_stacking,
                          t.updated_at
                   FROM targets t LEFT JOIN scopes s ON s.ip = t.scope_ip
                   ORDER BY s.hostname, t.target"""
            ).fetchall()
        return [dict(r) for r in rows]

    # -- runs (audit log) ---------------------------------------------------
    def start_run(self, kind: str, triggered_by: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO runs (kind, triggered_by, started_at, status)
                   VALUES (?, ?, ?, 'running')""",
                (kind, triggered_by, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE runs SET finished_at=?, status=?, summary_json=?
                   WHERE id=?""",
                (_now(), status, json.dumps(summary, default=str), run_id),
            )
            self._conn.commit()

    def recent_runs(self, limit: int = 25) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["summary"] = json.loads(d.pop("summary_json") or "null")
            except (ValueError, TypeError):
                d["summary"] = None
            out.append(d)
        return out

    def had_successful_run_today(self, triggered_by: str = "auto") -> bool:
        """True if a stack/upload auto-run finished OK since local midnight."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM runs
                   WHERE triggered_by=? AND status='ok'
                     AND kind IN ('stack','upload')
                     AND finished_at LIKE ?""",
                (triggered_by, f"{today}%"),
            ).fetchone()
        return bool(row and row["n"] > 0)

    # -- uploads ------------------------------------------------------------
    def log_upload(self, chunk_key, object_name, scope_ip, filename,
                   status="ok") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO uploads (chunk_key, object_name, scope_ip,
                        filename, uploaded_at, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chunk_key, object_name, scope_ip, filename, _now(), status),
            )
            self._conn.commit()

    # -- meta ---------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default
