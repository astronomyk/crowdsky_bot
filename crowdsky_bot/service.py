"""Application assembly: build the shared context, wire up seestarpy, start the
background threads, and serve the web app (DESIGN.md §4)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import scopes as scopes_mod
from .cache import Cache
from .config import Config, load_config
from .scheduler import Scheduler
from .worker import Job, Worker

log = logging.getLogger(__name__)


class Context:
    """Shared state handed to the worker, scheduler and web layer."""

    def __init__(self, cfg: Config, cache: Cache):
        self.cfg = cfg
        self.cache = cache
        self.scopes: list[dict] = []
        self.location = {"lon": 0.0, "lat": 0.0, "timezone": ""}
        self.worker: Worker | None = None
        self.scheduler: Scheduler | None = None

    def scope_ips(self) -> list[str]:
        return [s["ip"] for s in self.scopes]


def _db_path(cfg: Config) -> Path:
    return cfg.path.parent / "state.db"


def _apply_crowdsky_config(cfg: Config) -> None:
    from seestarpy import connection, crowdsky
    connection.VERBOSE_LEVEL = 0
    crowdsky.set_base_url(cfg.get("crowdsky.base_url"))
    user = cfg.get("crowdsky.username")
    pw = cfg.get("crowdsky.password")
    if user and pw:
        crowdsky.set_credentials(user, pw)
        os.environ["CROWDSKY_USERNAME"] = user
        os.environ["CROWDSKY_PASSWORD"] = pw


def refresh_scopes(ctx: Context) -> None:
    """(Re)discover scopes and sync location/timezone. Safe to call anytime."""
    cfg = ctx.cfg
    _apply_crowdsky_config(cfg)

    scopes = scopes_mod.discover(cfg)
    location = scopes_mod.read_location(cfg, [s["ip"] for s in scopes])
    ctx.location = location
    if location.get("timezone"):
        scopes_mod.apply_timezone(location["timezone"])

    for s in scopes:
        meta = scopes_mod.scope_metadata(s["ip"])
        s.update(meta)
        ctx.cache.upsert_scope(
            ip=s["ip"], hostname=s["hostname"], name=cfg.scope_name(s["ip"]),
            firmware=meta.get("firmware", ""),
            lon=location.get("lon"), lat=location.get("lat"),
        )
    ctx.scopes = scopes
    ctx.cache.set_meta("location", f'{location.get("lat")},{location.get("lon")}')
    ctx.cache.set_meta("timezone", location.get("timezone", ""))
    log.info("Context: %d scope(s), location=%s tz=%s",
             len(scopes), (location.get("lat"), location.get("lon")),
             location.get("timezone"))


def build_context(config_path: Path | None = None, discover: bool = True) -> Context:
    cfg = load_config(config_path)
    cache = Cache(_db_path(cfg))
    ctx = Context(cfg, cache)
    if discover:
        try:
            refresh_scopes(ctx)
        except Exception:  # noqa: BLE001
            log.exception("scope discovery failed at startup; continuing")
    else:
        _apply_crowdsky_config(cfg)
    return ctx


def start_background(ctx: Context) -> None:
    ctx.worker = Worker(ctx)
    ctx.worker.start()
    ctx.scheduler = Scheduler(ctx)
    ctx.scheduler.start()
    ctx.worker.enqueue(Job("refresh", triggered_by="auto"))


def serve(config_path: Path | None = None) -> None:
    """Entry point for ``crowdsky-bot serve``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ctx = build_context(config_path)
    start_background(ctx)

    from waitress import serve as waitress_serve

    from .web.app import create_app
    app = create_app(ctx)
    host = ctx.cfg.get("web.host", "0.0.0.0")
    port = int(ctx.cfg.get("web.port", 8080))
    log.info("Serving CrowdSky bot UI on http://%s:%d", host, port)
    waitress_serve(app, host=host, port=port, threads=4)
