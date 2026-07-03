"""Structured job wrappers over ``seestarpy.crowdsky`` (DESIGN.md §5).

Each job takes the config, cache, a progress reporter, a scope selection and a
target selection, and returns a JSON-serialisable summary. The worker calls
these one at a time; ``stack`` and ``upload`` fan out across scopes internally.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from seestarpy import connection, crowdsky, data, raw
from seestarpy.crowdsky import server as cs_server

from . import storage

log = logging.getLogger(__name__)

_ALL = (None, "all", ["all"])


def apply_credentials(cfg) -> bool:
    """Push CrowdSky base URL + credentials into seestarpy's module globals.

    ``crowdsky.list_stacks`` / ``upload_stack`` read the username/password from
    the ``crowdsky.server`` module (set via ``set_credentials``), NOT from our
    config. This must be called whenever the config changes and before any job
    that hits the server, otherwise seestarpy raises "credentials not set".
    Returns True if credentials were present and applied.
    """
    connection.VERBOSE_LEVEL = 0
    base = cfg.get("crowdsky.base_url")
    if base:
        crowdsky.set_base_url(base)
    user = cfg.get("crowdsky.username")
    pw = cfg.get("crowdsky.password")
    if user and pw:
        crowdsky.set_credentials(user, pw)
        os.environ["CROWDSKY_USERNAME"] = user
        os.environ["CROWDSKY_PASSWORD"] = pw
        return True
    return False


# ---------------------------------------------------------------------------
# Progress reporter (the worker passes its own; run-once passes NullProgress)
# ---------------------------------------------------------------------------
class NullProgress:
    def set_message(self, msg: str) -> None:  # noqa: D401
        log.info("%s", msg)

    def set_scope(self, ip: str, msg: str) -> None:
        log.info("[%s] %s", ip, msg)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _eligible(block: dict, min_exptime: float) -> bool:
    try:
        exp = float(block["exposure"].rstrip("s"))
    except (KeyError, ValueError):
        return False
    return block.get("frame_count", 0) * exp >= min_exptime


def _is_observing(ip: str) -> bool:
    """True if the scope is actively exposing (don't stack mid-session)."""
    try:
        resp = raw.get_view_state(ips=ip)
        view = (resp or {}).get("result", {}).get("View", {})
        return view.get("state") == "working"
    except Exception as exc:  # noqa: BLE001
        log.debug("get_view_state failed on %s: %s", ip, exc)
        return False  # can't tell -> don't block stacking


def _object_from_filename(fname: str) -> str | None:
    m = crowdsky.CROWDSKY_RE.match(fname) or crowdsky.CROWDSKY_RE_LEGACY.match(fname)
    return m.group(2) if m else None


def _safe_dir(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "scope"


def _scope_target_names(ip: str, cfg, require_raw: bool = False) -> list[str]:
    """Target names present on *ip*, minus excluded folders."""
    patterns = cfg.exclude_patterns
    names = []
    for t in crowdsky.list_targets(ips=ip):
        if _excluded(t["target"], patterns):
            continue
        if require_raw and t.get("raw_files", 0) <= 0:
            continue
        names.append(t["target"])
    return names


def _resolve_targets(ip, targets, cfg, require_raw=False) -> list[str]:
    if targets in _ALL:
        return _scope_target_names(ip, cfg, require_raw=require_raw)
    return [t for t in targets if not _excluded(t, cfg.exclude_patterns)]


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------
def refresh(cfg, cache, progress, scope_ips, dry_run=False) -> dict:
    apply_credentials(cfg)
    patterns = cfg.exclude_patterns
    block_minutes = int(cfg.get("stacking.block_minutes", 15))
    min_exptime = float(cfg.get("stacking.min_exptime", 240))

    # -- server side, once --
    server_counts: dict[str, int] = {}
    server_keys: set[str] = set()
    if cfg.get("crowdsky.username") and cfg.get("crowdsky.password"):
        try:
            for s in crowdsky.list_stacks():
                obj = s.get("object_name")
                if obj:
                    server_counts[obj] = server_counts.get(obj, 0) + 1
                key = s.get("chunk_key")
                if key:
                    server_keys.add(key)
        except Exception as exc:  # noqa: BLE001
            progress.set_message(f"server list_stacks failed: {exc}")
    else:
        progress.set_message("no CrowdSky credentials — skipping server counts")

    summary = {"scopes": {}, "targets": 0}
    for ip in scope_ips:
        progress.set_scope(ip, "listing targets")
        try:
            tlist = crowdsky.list_targets(ips=ip)
        except Exception as exc:  # noqa: BLE001
            progress.set_scope(ip, f"error: {exc}")
            summary["scopes"][ip] = {"error": str(exc)}
            continue

        keep: set[str] = set()
        for t in tlist:
            tgt = t["target"]
            if _excluded(tgt, patterns):
                continue
            keep.add(tgt)
            progress.set_scope(ip, f"scanning {tgt}")

            on_server = server_counts.get(tgt, 0)
            awaiting_upload = 0
            try:
                files = data.list_folder_contents(tgt, filetype="fit", ips=ip)
                keys = [cs_server._parse_chunk_key(f)
                        for f in files if f.startswith("CrowdSky_")]
                awaiting_upload = sum(1 for k in keys if k and k not in server_keys)
            except Exception as exc:  # noqa: BLE001
                log.debug("local fit scan failed for %s/%s: %s", ip, tgt, exc)

            awaiting_stacking = 0
            if t.get("raw_files", 0) > 0:
                try:
                    blocks = crowdsky.find_unstacked_blocks(
                        tgt, block_minutes=block_minutes, ips=ip)
                    awaiting_stacking = sum(1 for b in blocks
                                            if _eligible(b, min_exptime))
                except Exception as exc:  # noqa: BLE001
                    log.debug("find_unstacked_blocks failed for %s/%s: %s",
                              ip, tgt, exc)

            cache.upsert_target(ip, tgt, on_server, awaiting_upload,
                                awaiting_stacking)
            summary["targets"] += 1

        cache.prune_targets(ip, keep)
        summary["scopes"][ip] = len(keep)
        progress.set_scope(ip, "done")

    cache.set_meta("last_refreshed", _now())
    return summary


# ---------------------------------------------------------------------------
# stack (parallel across scopes)
# ---------------------------------------------------------------------------
def stack(cfg, cache, progress, scope_ips, targets, dry_run=False) -> dict:
    block_minutes = int(cfg.get("stacking.block_minutes", 15))
    min_exptime = float(cfg.get("stacking.min_exptime", 240))

    def do_scope(ip: str) -> dict:
        result = {"ip": ip, "targets": []}
        observing = _is_observing(ip)
        if observing and not dry_run:
            progress.set_scope(ip, "still observing — skipped")
            result["skipped"] = "observing"
            return result
        if observing:
            progress.set_scope(ip, "still observing (preview only)")

        names = _resolve_targets(ip, targets, cfg, require_raw=True)
        for tgt in names:
            progress.set_scope(ip, f"stacking {tgt}")
            try:
                r = crowdsky.stack_blocks(
                    tgt, block_minutes=block_minutes, min_exptime=min_exptime,
                    dry_run=dry_run, ips=ip)
            except Exception as exc:  # noqa: BLE001
                r = {"target": tgt, "error": str(exc)}
            result["targets"].append(r)
        progress.set_scope(ip, "done")
        return result

    if not scope_ips:
        return {"scopes": []}
    with ThreadPoolExecutor(max_workers=len(scope_ips)) as ex:
        results = list(ex.map(do_scope, scope_ips))
    return {"dry_run": dry_run, "scopes": results}


# ---------------------------------------------------------------------------
# upload (parallel across scopes) + retention
# ---------------------------------------------------------------------------
def upload(cfg, cache, progress, scope_ips, targets, dry_run=False) -> dict:
    apply_credentials(cfg)
    stacks_dir = cfg.stacks_dir

    def do_scope(ip: str) -> dict:
        name = cfg.scope_name(ip)
        dest = stacks_dir / _safe_dir(name)
        # If excludes are configured and caller asked for "all", resolve names
        # so excluded folders aren't uploaded (upload_all_stacks can't filter).
        if targets in _ALL and cfg.exclude_patterns:
            target_arg = _resolve_targets(ip, "all", cfg)
        elif targets in _ALL:
            target_arg = "all"
        else:
            target_arg = list(targets)

        progress.set_scope(ip, "uploading")
        try:
            r = crowdsky.upload_all_stacks(
                target=target_arg, dest=str(dest), skip_existing=True,
                dry_run=dry_run, ips=ip)
        except Exception as exc:  # noqa: BLE001
            progress.set_scope(ip, f"error: {exc}")
            return {"ip": ip, "error": str(exc)}

        if not dry_run:
            for fname in r.get("uploaded", []):
                cache.log_upload(cs_server._parse_chunk_key(fname),
                                 _object_from_filename(fname), ip, fname)
        progress.set_scope(ip, "done")
        return {
            "ip": ip,
            "uploaded": r.get("files_uploaded", 0),
            "skipped": r.get("files_skipped", 0),
            "failed": r.get("files_failed", 0),
            "uploaded_files": r.get("uploaded", []),
        }

    if not scope_ips:
        return {"scopes": []}
    with ThreadPoolExecutor(max_workers=len(scope_ips)) as ex:
        results = list(ex.map(do_scope, scope_ips))

    out = {"dry_run": dry_run, "scopes": results}
    if not dry_run:
        progress.set_message("enforcing SD retention")
        out["retention"] = storage.enforce_retention(
            stacks_dir,
            float(cfg.get("storage.reserve_gb_per_scope", 1.0)),
            max(1, len(scope_ips)),
        )
    return out


# ---------------------------------------------------------------------------
# purge (destructive)
# ---------------------------------------------------------------------------
def purge(cfg, cache, progress, scope_ips, targets, dry_run=False) -> dict:
    results = []
    for ip in scope_ips:
        names = None if targets in _ALL else _resolve_targets(ip, targets, cfg)
        if dry_run:
            progress.set_scope(ip, "would purge (dry run)")
            results.append({"ip": ip, "would_purge": names or "all"})
            continue
        progress.set_scope(ip, "purging CrowdSky stacks")
        try:
            if names is None:
                r = crowdsky.purge_crowdsky_stacks(folder=None, ips=ip)
                results.append({"ip": ip, **_purge_summary(r)})
            else:
                total = {"files_deleted": 0, "folders_scanned": 0}
                for tgt in names:
                    r = crowdsky.purge_crowdsky_stacks(folder=tgt, ips=ip)
                    total["files_deleted"] += r.get("files_deleted", 0)
                    total["folders_scanned"] += r.get("folders_scanned", 0)
                results.append({"ip": ip, **total})
        except Exception as exc:  # noqa: BLE001
            results.append({"ip": ip, "error": str(exc)})
    return {"scopes": results}


def _purge_summary(r: dict) -> dict:
    return {
        "files_deleted": r.get("files_deleted", 0),
        "folders_scanned": r.get("folders_scanned", 0),
    }


# ---------------------------------------------------------------------------
# gallery helper (used by the web layer)
# ---------------------------------------------------------------------------
def list_gallery(ip: str, target: str) -> list[str]:
    """Return preview-image filenames for a target on a scope.

    Lists the folder over **SMB** rather than the JSON-RPC file API: on current
    firmware the RPC (``data.list_folder_contents``) only enumerates ``.fit``
    files and never returns the ``.jpg`` / ``_thn.jpg`` previews, even though
    they exist on disk. SMB sees every file. Prefers thumbnails, falls back to
    full-size JPEGs, then PNGs.
    """
    from smb.SMBConnection import SMBConnection

    conn = SMBConnection("", "", "crowdsky-bot", "seestar",
                         use_ntlm_v2=False, is_direct_tcp=True)
    try:
        conn.connect(ip, 445)
        entries = conn.listPath(data.SHARE_NAME, f"{data.ROOT_DIR}/{target}")
        names = [e.filename for e in entries if not e.isDirectory]
    except Exception as exc:  # noqa: BLE001
        log.warning("gallery SMB listing failed for %s/%s: %s", ip, target, exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    thumbs = sorted(n for n in names if n.endswith("_thn.jpg"))
    if thumbs:
        return thumbs
    jpgs = sorted(n for n in names if n.lower().endswith(".jpg"))
    if jpgs:
        return jpgs
    return sorted(n for n in names if n.lower().endswith(".png"))
