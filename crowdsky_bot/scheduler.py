"""Dawn scheduler (DESIGN.md §6).

Fires the nightly stack+upload pass at the configured trigger (default
``sunrise - 30 min``). Handles boot catch-up and waits for NTP before firing so
a Pi Zero's RTC-less clock can't skew dawn maths or chunk keys.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import dawn, sunrise

from . import scopes as scopes_mod
from .worker import Job

log = logging.getLogger(__name__)


def _tz(cfg, location) -> object:
    name = (location or {}).get("timezone") or cfg.get("location.timezone")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            log.warning("Unknown timezone %r; using system local.", name)
    return datetime.now().astimezone().tzinfo


def next_trigger(cfg, location, now: datetime | None = None) -> datetime:
    """Return the next trigger datetime (tz-aware) after *now*."""
    tz = _tz(cfg, location)
    now = now or datetime.now(tz)
    trig = cfg.get("schedule.trigger", "sunrise")
    offset = timedelta(minutes=int(cfg.get("schedule.offset_minutes", -30)))
    obs = Observer(latitude=float(location["lat"]),
                   longitude=float(location["lon"]))

    def compute_for(date) -> datetime:
        if trig == "fixed":
            hh, mm = (cfg.get("schedule.fixed_time", "09:00") + ":0").split(":")[:2]
            return datetime(date.year, date.month, date.day,
                            int(hh), int(mm), tzinfo=tz)
        if trig == "astro_dawn":
            try:
                return dawn(obs, date=date, tzinfo=tz, depression=18) + offset
            except Exception:  # noqa: BLE001 - polar summer: no astro dawn
                return sunrise(obs, date=date, tzinfo=tz) + offset
        return sunrise(obs, date=date, tzinfo=tz) + offset

    for delta in range(0, 4):
        cand = compute_for((now + timedelta(days=delta)).date())
        if cand > now:
            return cand
    # Fallback (shouldn't happen)
    return compute_for((now + timedelta(days=1)).date())


class Scheduler(threading.Thread):
    def __init__(self, ctx):
        super().__init__(daemon=True, name="crowdsky-scheduler")
        self.ctx = ctx
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- firing -------------------------------------------------------------
    def _fire(self, triggered_by: str) -> None:
        cfg = self.ctx.cfg
        log.info("Scheduler firing nightly pass (%s)", triggered_by)
        if cfg.get("schedule.auto_stack", True):
            self.ctx.worker.enqueue(Job("stack", triggered_by=triggered_by))
        if cfg.get("schedule.auto_upload", True):
            self.ctx.worker.enqueue(Job("upload", triggered_by=triggered_by))

    def _wait_for_clock(self) -> None:
        """Block until NTP reports synced (or the state is unknowable)."""
        while not self._stop.is_set():
            synced = scopes_mod.ntp_synced()
            if synced is None or synced:
                return
            log.info("Waiting for NTP sync before scheduling…")
            self._stop.wait(30)

    def _maybe_catch_up(self) -> None:
        cfg = self.ctx.cfg
        if not cfg.get("schedule.catch_up_on_boot", True):
            return
        tz = _tz(cfg, self.ctx.location)
        now = datetime.now(tz)
        today_trigger = next_trigger(
            cfg, self.ctx.location,
            now=datetime(now.year, now.month, now.day, tzinfo=tz),
        )
        if now > today_trigger and not self.ctx.cache.had_successful_run_today("auto"):
            log.info("Boot catch-up: today's trigger already passed, firing now.")
            self._fire("auto")

    # -- main loop ----------------------------------------------------------
    def run(self) -> None:
        self._wait_for_clock()
        if self._stop.is_set():
            return
        try:
            self._maybe_catch_up()
        except Exception:  # noqa: BLE001
            log.exception("catch-up check failed")

        while not self._stop.is_set():
            tz = _tz(self.ctx.cfg, self.ctx.location)
            try:
                nxt = next_trigger(self.ctx.cfg, self.ctx.location)
            except Exception:  # noqa: BLE001
                log.exception("could not compute next trigger; retrying in 5 min")
                self._stop.wait(300)
                continue
            log.info("Next trigger at %s", nxt.isoformat())

            # Sleep until the trigger, waking every 30 s (config may change).
            while not self._stop.is_set():
                remaining = (nxt - datetime.now(tz)).total_seconds()
                if remaining <= 0:
                    break
                self._stop.wait(min(30, remaining))
            if self._stop.is_set():
                return

            self._fire("auto")
            self._stop.wait(90)  # don't immediately recompute onto the same instant
