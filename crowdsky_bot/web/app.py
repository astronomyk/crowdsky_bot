"""Flask web app: single page + JSON API + Seestar thumbnail proxy.

Server-rendered shell; ``static/app.js`` polls the JSON endpoints. No CDN, no
build step — works offline on the LAN.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse

import requests
from flask import (Flask, Response, jsonify, render_template, request,
                   stream_with_context)

from .. import jobs as jobs_mod
from ..worker import Job

log = logging.getLogger(__name__)


def _selection(value):
    """Normalise a scopes/targets selection from a request body."""
    if value in (None, "all", ["all"], []):
        return "all"
    if isinstance(value, str):
        return [value]
    return list(value)


def create_app(ctx) -> Flask:
    app = Flask(__name__)

    def worker() -> "object":
        return ctx.worker

    # -- page ---------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            config=ctx.cfg.redacted(),
            scopes=ctx.scopes,
            location=ctx.location,
        )

    # -- status / summary ---------------------------------------------------
    @app.get("/api/status")
    def api_status():
        prog = worker().progress.snapshot() if worker() else {"state": "idle"}
        return jsonify({
            "progress": prog,
            "scopes": [
                {"ip": s["ip"], "hostname": s.get("hostname"),
                 "name": s.get("name") or ctx.cfg.scope_name(s["ip"]),
                 "firmware": s.get("firmware", "")}
                for s in ctx.scopes
            ],
            "runs": ctx.cache.recent_runs(10),
        })

    @app.get("/api/summary")
    def api_summary():
        return jsonify({
            "rows": ctx.cache.get_summary(),
            "last_refreshed": ctx.cache.get_meta("last_refreshed"),
        })

    @app.get("/api/runs")
    def api_runs():
        return jsonify(ctx.cache.recent_runs(50))

    # -- triggers -----------------------------------------------------------
    def _enqueue(kind: str):
        body = request.get_json(silent=True) or {}
        job = Job(
            kind=kind,
            triggered_by="manual",
            scopes=_selection(body.get("scopes")),
            targets=_selection(body.get("targets")),
            dry_run=bool(body.get("dry_run", False)),
        )
        queued = worker().enqueue(job)
        return jsonify({"queued": queued, "kind": kind})

    @app.post("/api/refresh")
    def api_refresh():
        queued = worker().enqueue(Job("refresh", triggered_by="manual"))
        return jsonify({"queued": queued, "kind": "refresh"})

    @app.post("/api/stack")
    def api_stack():
        return _enqueue("stack")

    @app.post("/api/upload")
    def api_upload():
        return _enqueue("upload")

    @app.post("/api/purge")
    def api_purge():
        body = request.get_json(silent=True) or {}
        if not body.get("confirm"):
            return jsonify({"error": "purge requires confirm=true"}), 400
        job = Job("purge", triggered_by="manual",
                  scopes=_selection(body.get("scopes")),
                  targets=_selection(body.get("targets")))
        return jsonify({"queued": worker().enqueue(job), "kind": "purge"})

    # -- config -------------------------------------------------------------
    @app.get("/api/config")
    def api_get_config():
        return jsonify(ctx.cfg.redacted())

    @app.post("/api/config")
    def api_set_config():
        incoming = request.get_json(silent=True) or {}
        # Don't clobber secrets with their redacted placeholders.
        cs = incoming.get("crowdsky", {})
        if cs.get("password") in ("********", None):
            cs.pop("password", None)
        web = incoming.get("web", {})
        if web.get("pin") in ("****", None):
            web.pop("pin", None)

        before = (ctx.cfg.get("scopes.count"), ctx.cfg.get("scopes.max_probe"),
                  ctx.cfg.get("location.source"), ctx.cfg.get("location.lat"),
                  ctx.cfg.get("location.lon"), ctx.cfg.get("location.timezone"))
        ctx.cfg.update(incoming)
        ctx.cfg.save()
        # Push credentials/base-url into seestarpy immediately so the next job
        # doesn't fail with "credentials not set".
        jobs_mod.apply_credentials(ctx.cfg)
        after = (ctx.cfg.get("scopes.count"), ctx.cfg.get("scopes.max_probe"),
                 ctx.cfg.get("location.source"), ctx.cfg.get("location.lat"),
                 ctx.cfg.get("location.lon"), ctx.cfg.get("location.timezone"))

        if before != after:
            # Re-discover / re-sync location off the request thread.
            from .. import service
            threading.Thread(
                target=lambda: service.refresh_scopes(ctx),
                name="rediscover", daemon=True,
            ).start()
        return jsonify(ctx.cfg.redacted())

    # -- gallery + thumbnail proxy -----------------------------------------
    @app.get("/api/gallery")
    def api_gallery():
        scope = request.args.get("scope", "")
        target = request.args.get("target", "")
        if scope not in ctx.scope_ips():
            return jsonify({"error": "unknown scope"}), 400
        names = jobs_mod.list_gallery(scope, target)
        base = f"/proxy/{scope}/" + urllib.parse.quote(f"MyWorks/{target}")
        return jsonify({
            "images": [{"name": n, "url": f"{base}/{urllib.parse.quote(n)}"}
                       for n in names]
        })

    @app.get("/proxy/<scope_ip>/<path:p>")
    def proxy(scope_ip: str, p: str):
        if scope_ip not in ctx.scope_ips():
            return Response("unknown scope", status=400)
        if not p.startswith("MyWorks/"):
            return Response("forbidden path", status=403)
        url = f"http://{scope_ip}/" + urllib.parse.quote(p, safe="/")
        try:
            upstream = requests.get(url, stream=True, timeout=15)
        except requests.RequestException as exc:
            return Response(f"scope unreachable: {exc}", status=502)
        if upstream.status_code != 200:
            return Response("not found", status=upstream.status_code)
        ctype = upstream.headers.get("Content-Type", "application/octet-stream")

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                yield chunk

        return Response(stream_with_context(generate()), content_type=ctype)

    return app
