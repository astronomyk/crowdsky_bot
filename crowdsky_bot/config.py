"""Persistent configuration for the CrowdSky bot.

The config is a TOML file on the SD card (survives reboot / replug). It holds
the CrowdSky password, so the file is created ``0600`` in a ``0700`` dir.

Read with :func:`load_config`, mutate the returned :class:`Config` in place (or
via :meth:`Config.set`), and persist with :meth:`Config.save`. Access nested
values with dotted keys: ``cfg.get("crowdsky.username")``.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

# ---------------------------------------------------------------------------
# Defaults — the full schema lives here (see DESIGN.md §3).
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "crowdsky": {
        "username": "",
        "password": "",
        "base_url": "https://crowdsky.univie.ac.at",
    },
    "scopes": {
        "count": 0,        # 0 = auto-discover
        "max_probe": 8,    # how far to probe when auto-discovering
        "names": {},       # {ip: "friendly name"}
    },
    "location": {
        "source": "seestar",   # "seestar" | "manual"
        "lat": 0.0,
        "lon": 0.0,
        "timezone": "",        # IANA; empty => read from scope / keep Pi default
    },
    "schedule": {
        "auto_stack": True,
        "auto_upload": True,
        "trigger": "sunrise",       # "sunrise" | "astro_dawn" | "fixed"
        "offset_minutes": -30,      # sunrise - 30 min
        "fixed_time": "09:00",
        "catch_up_on_boot": True,
    },
    "stacking": {
        "block_minutes": 15,
        "min_exptime": 240,
        "exclude_patterns": [],     # fnmatch patterns of folders to skip
    },
    "storage": {
        "stacks_dir": "~/crowdsky_stacks",
        "reserve_gb_per_scope": 1.0,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "auth": "none",             # "none" | "pin"
        "pin": "",
    },
}


def default_config_path() -> Path:
    """Return the config path, honouring ``$CROWDSKY_BOT_CONFIG``."""
    env = os.environ.get("CROWDSKY_BOT_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "crowdsky_bot" / "config.toml"


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge *over* into a copy of *base*."""
    out = copy.deepcopy(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


class Config:
    """A loaded config backed by a TOML file."""

    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path

    # -- dotted access ------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def update(self, incoming: dict) -> None:
        """Deep-merge a (partial) config dict into this one."""
        self.data = _deep_merge(self.data, incoming)

    # -- typed helpers used across the app ----------------------------------
    @property
    def stacks_dir(self) -> Path:
        return Path(self.get("storage.stacks_dir")).expanduser()

    @property
    def exclude_patterns(self) -> list[str]:
        return list(self.get("stacking.exclude_patterns") or [])

    def scope_name(self, ip: str) -> str:
        return (self.get("scopes.names") or {}).get(ip, ip)

    def redacted(self) -> dict:
        """A copy of the config with secrets masked, for the API."""
        out = copy.deepcopy(self.data)
        if out.get("crowdsky", {}).get("password"):
            out["crowdsky"]["password"] = "********"
        if out.get("web", {}).get("pin"):
            out["web"]["pin"] = "****"
        return out

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass  # e.g. Windows dev box
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            tomli_w.dump(self.data, fh)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


def load_config(path: Path | None = None) -> Config:
    """Load config from *path* (or the default), creating defaults if missing."""
    path = path or default_config_path()
    if path.exists():
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        data = _deep_merge(DEFAULTS, raw)
        cfg = Config(data, path)
    else:
        cfg = Config(copy.deepcopy(DEFAULTS), path)
        cfg.save()
    return cfg
