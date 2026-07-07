"""Seestar discovery and location / timezone synchronisation.

Everything here talks to the scopes via seestarpy. seestarpy submodules are
imported lazily inside the functions so this module (and the pure-logic tests)
can be imported on a machine where seestarpy isn't installed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)


def _valid_scopes(available: dict) -> list[dict]:
    """Scopes from AVAILABLE_IPS, dropping the AP-hotspot fallback (10.0.0.1)."""
    seen = set()
    out = []
    for host, ip in available.items():
        if not ip or ip == "10.0.0.1" or ip in seen:
            continue
        seen.add(ip)
        out.append({"ip": ip, "hostname": host})
    out.sort(key=lambda s: s["hostname"])
    return out


def discover(cfg) -> list[dict]:
    """Discover Seestars on the LAN via mDNS.

    Probes ``seestar.local``, ``seestar-2.local`` … up to ``scopes.count`` (or
    ``scopes.max_probe`` when count is 0 / auto). mDNS resolution is flaky under
    a short deadline, so we retry a few times with a generous timeout and union
    the results (``find_available_ips`` accumulates into ``AVAILABLE_IPS``).
    Returns a sorted list of ``{"ip", "hostname"}`` dicts.
    """
    from seestarpy import connection as conn

    count = int(cfg.get("scopes.count") or 0)
    n = count if count > 0 else int(cfg.get("scopes.max_probe") or 8)
    timeout = float(cfg.get("scopes.discover_timeout", 5))
    attempts = int(cfg.get("scopes.discover_attempts", 3))

    scopes: list[dict] = []
    prev = -1
    for i in range(max(1, attempts)):
        conn.find_available_ips(n, timeout=timeout)
        scopes = _valid_scopes(conn.AVAILABLE_IPS)
        if count > 0 and len(scopes) >= count:
            break
        # Auto mode: stop once the count stabilises across two passes.
        if count == 0 and i >= 1 and len(scopes) == prev:
            break
        prev = len(scopes)
        if i + 1 < attempts:
            log.info("Discovery pass %d found %d scope(s); retrying…",
                     i + 1, len(scopes))

    log.info("Discovered %d scope(s): %s", len(scopes),
             ", ".join(f"{s['hostname']}={s['ip']}" for s in scopes))
    return scopes


def format_scope_label(meta: dict, ip: str) -> str:
    """Human label: '<sn last 3> <model> (.<last IP octet>)', e.g. '936 S50 (.81)'."""
    sn = (meta.get("sn") or "")
    sn3 = sn[-3:] if sn else "???"
    model = (meta.get("model") or "").replace("Seestar", "").strip()
    octet = ip.split(".")[-1]
    label = sn3
    if model:
        label += f" {model}"
    return f"{label} (.{octet})"


def scope_metadata(ip: str) -> dict:
    """Return device + storage metadata for a scope (best-effort).

    Keys: ``firmware, model, sn, storage_used_pct, storage_free_mb,
    storage_total_mb`` (storage values may be ``None`` if unavailable).
    """
    from seestarpy import raw
    meta = {
        "firmware": "", "model": "", "sn": "",
        "storage_used_pct": None, "storage_free_mb": None,
        "storage_total_mb": None,
    }
    try:
        resp = raw.get_device_state(keys=["device", "storage"], ips=ip)
        result = (resp or {}).get("result", {})
        dev = result.get("device", {})
        meta["firmware"] = dev.get("firmware_ver_string", "")
        meta["model"] = dev.get("product_model", "")
        meta["sn"] = dev.get("sn", "")
        vol = (result.get("storage", {}).get("storage_volume") or [{}])[0]
        meta["storage_used_pct"] = vol.get("used_percent")
        meta["storage_free_mb"] = vol.get("free_mb")
        meta["storage_total_mb"] = vol.get("total_mb")
    except Exception as exc:  # noqa: BLE001 - metadata is optional
        log.warning("Could not read device state from %s: %s", ip, exc)
    return meta


def read_location(cfg, scope_ips: list[str]) -> dict:
    """Resolve the observing location + timezone.

    Returns ``{"lon", "lat", "timezone", "source"}``. ``timezone`` may be an
    empty string if it could not be determined.

    NOTE: the Seestar reports coordinates as ``[lon, lat]`` (not lat, lon).
    """
    source = cfg.get("location.source", "seestar")
    if source == "manual":
        return {
            "lon": float(cfg.get("location.lon") or 0.0),
            "lat": float(cfg.get("location.lat") or 0.0),
            "timezone": cfg.get("location.timezone") or "",
            "source": "manual",
        }

    lon = lat = None
    tz = cfg.get("location.timezone") or ""
    for ip in scope_ips:
        lonlat = _read_lonlat(ip)
        if lonlat is not None:
            lon, lat = lonlat
            if not tz:
                tz = _read_timezone(ip) or ""
            break

    if lon is None:
        # Fall back to whatever is configured (possibly zeros).
        log.warning("Could not read location from any scope; using config.")
        return {
            "lon": float(cfg.get("location.lon") or 0.0),
            "lat": float(cfg.get("location.lat") or 0.0),
            "timezone": tz,
            "source": "seestar",
        }

    return {"lon": lon, "lat": lat, "timezone": tz, "source": "seestar"}


def _read_lonlat(ip: str):
    """Return ``(lon, lat)`` from a scope, or None."""
    from seestarpy import raw
    try:
        resp = raw.get_device_state(keys=["location_lon_lat"], ips=ip)
        val = (resp or {}).get("result", {}).get("location_lon_lat")
        if isinstance(val, (list, tuple)) and len(val) == 2:
            return float(val[0]), float(val[1])
    except Exception as exc:  # noqa: BLE001
        log.debug("get_device_state(location) failed on %s: %s", ip, exc)
    try:
        resp = raw.get_user_location(ips=ip)
        val = (resp or {}).get("result")
        if isinstance(val, (list, tuple)) and len(val) == 2:
            return float(val[0]), float(val[1])
    except Exception as exc:  # noqa: BLE001
        log.debug("get_user_location failed on %s: %s", ip, exc)
    return None


def _read_timezone(ip: str) -> str | None:
    """Try to read an IANA timezone string the scope knows about."""
    from seestarpy import raw
    try:
        resp = raw.pi_get_time(ips=ip)
        result = (resp or {}).get("result", {})
        if isinstance(result, dict):
            for key in ("time_zone", "timezone", "tz"):
                if result.get(key):
                    return str(result[key])
    except Exception as exc:  # noqa: BLE001
        log.debug("pi_get_time failed on %s: %s", ip, exc)
    return None


def apply_timezone(tz: str) -> bool:
    """Set the host OS timezone to *tz* so chunk keys are computed correctly.

    ``crowdsky.local_dt_to_chunk_str`` interprets the Seestar's filename
    timestamps in the host's local timezone, so the Pi's tz must match the
    observing site. Best-effort: uses ``timedatectl`` (needs privilege) and
    always sets ``$TZ`` for the running process as a fallback.

    Returns True if ``timedatectl`` succeeded.
    """
    if not tz:
        return False
    # Process-level fallback (always safe, POSIX-only tzset()).
    os.environ["TZ"] = tz
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception:  # noqa: BLE001
            pass
    try:
        subprocess.run(
            ["timedatectl", "set-timezone", tz],
            check=True, capture_output=True, timeout=10,
        )
        log.info("Set system timezone to %s", tz)
        return True
    except Exception as exc:  # noqa: BLE001 - non-Linux / no privilege
        log.warning("Could not set system timezone to %s (%s); using $TZ only.",
                    tz, exc)
        return False


def ntp_synced() -> bool | None:
    """Return True/False if NTP sync state is known, else None (can't tell)."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return out.lower() == "yes"
    except Exception:  # noqa: BLE001
        return None
