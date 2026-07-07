"""Local stack archive + free-space retention (DESIGN.md §5.1).

Uploaded stacks are kept under ``stacks_dir/<scope>/<target>/`` so the user can
browse them offline and to spread SD write wear. When free space drops below
``reserve_gb_per_scope * n_scopes`` the oldest stacks are pruned first.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# CrowdSky_<N>_<target>_<exp>_<filt>_<YYYYMMDD.CC>_HP<nnnnnn>.fit  (and legacy)
_KEY_RE = re.compile(r"_(\d{8})\.(\d{1,2})_HP\d{6}\.fit$")
_KEY_RE_LEGACY = re.compile(r"_(\d{8})-(\d{6})\.fit$")

_GB = 1024 ** 3


def _observation_sort_key(path: Path) -> tuple:
    """Sort key = observation time from the filename (oldest first).

    Falls back to file mtime when the name can't be parsed.
    """
    name = path.name
    m = _KEY_RE.search(name)
    if m:
        # chunk index 0..95 -> minutes; good enough for ordering
        return (m.group(1), int(m.group(2)))
    m = _KEY_RE_LEGACY.search(name)
    if m:
        return (m.group(1), int(m.group(2)))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return (datetime.fromtimestamp(mtime).strftime("%Y%m%d"), int(mtime) % 100000)


def _companions(fit_path: Path) -> list[Path]:
    stem = fit_path.name[:-4]  # drop ".fit"
    out = []
    for ext in (".jpg", "_thn.jpg"):
        p = fit_path.with_name(stem + ext)
        if p.exists():
            out.append(p)
    return out


def enforce_retention(stacks_dir: Path, reserve_gb_per_scope: float,
                      n_scopes: int) -> dict:
    """Prune oldest stacks until free space meets the reserve.

    Returns a summary ``{reserve_gb, free_gb_before, free_gb_after,
    files_deleted, deleted, bytes_freed, warning}``.
    """
    stacks_dir = Path(stacks_dir).expanduser()
    reserve_bytes = int(reserve_gb_per_scope * max(1, n_scopes) * _GB)
    summary = {
        "reserve_gb": round(reserve_bytes / _GB, 2),
        "files_deleted": 0,
        "deleted": [],
        "bytes_freed": 0,
        "warning": None,
    }

    if not stacks_dir.exists():
        summary["warning"] = "stacks_dir does not exist yet"
        return summary

    try:
        free_before = shutil.disk_usage(stacks_dir).free
    except OSError as exc:
        summary["warning"] = f"disk_usage failed: {exc}"
        return summary
    summary["free_gb_before"] = round(free_before / _GB, 2)

    if free_before >= reserve_bytes:
        summary["free_gb_after"] = summary["free_gb_before"]
        return summary

    fits = sorted(stacks_dir.rglob("CrowdSky_*.fit"), key=_observation_sort_key)
    for fit in fits:
        if shutil.disk_usage(stacks_dir).free >= reserve_bytes:
            break
        group = [fit, *_companions(fit)]
        for p in group:
            try:
                sz = p.stat().st_size
                p.unlink()
                summary["bytes_freed"] += sz
            except OSError as exc:
                log.warning("Could not delete %s: %s", p, exc)
        summary["files_deleted"] += 1
        summary["deleted"].append(fit.name)
        log.info("Retention: pruned %s", fit.name)

    free_after = shutil.disk_usage(stacks_dir).free
    summary["free_gb_after"] = round(free_after / _GB, 2)
    if free_after < reserve_bytes:
        summary["warning"] = (
            "Could not meet reserve even after pruning all stacks; "
            "something else is filling the card."
        )
    return summary
