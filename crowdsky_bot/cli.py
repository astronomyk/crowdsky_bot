"""Command-line entry point: ``crowdsky-bot <serve|run-once|status|install>``."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _config_path(args) -> Path | None:
    return Path(args.config).expanduser() if args.config else None


# ---------------------------------------------------------------------------
def cmd_serve(args) -> int:
    from . import service
    service.serve(_config_path(args))
    return 0


def cmd_run_once(args) -> int:
    """Exercise the nightly pass. Stacking/upload default to dry-run."""
    from . import jobs, service
    ctx = service.build_context(_config_path(args), discover=True)
    prog = jobs.NullProgress()
    dry = not args.execute

    print(f"# scopes: {ctx.scope_ips()}")
    print(f"# location: {ctx.location}")

    print("\n== refresh ==")
    print(json.dumps(jobs.refresh(ctx.cfg, ctx.cache, prog, ctx.scope_ips()),
                     indent=2, default=str))

    print(f"\n== stack (dry_run={dry}) ==")
    print(json.dumps(
        jobs.stack(ctx.cfg, ctx.cache, prog, ctx.scope_ips(), "all", dry_run=dry),
        indent=2, default=str))

    print(f"\n== upload (dry_run={dry}) ==")
    print(json.dumps(
        jobs.upload(ctx.cfg, ctx.cache, prog, ctx.scope_ips(), "all", dry_run=dry),
        indent=2, default=str))
    return 0


def cmd_status(args) -> int:
    from . import service
    ctx = service.build_context(_config_path(args), discover=False)
    print("Scopes (cached):")
    for s in ctx.cache.get_scopes():
        print(f"  {s['hostname']:20} {s['ip']:16} fw={s['firmware']}")
    print("\nRecent runs:")
    for r in ctx.cache.recent_runs(10):
        print(f"  #{r['id']:<4} {r['kind']:8} {r['status']:8} "
              f"{r['triggered_by']:8} {r.get('finished_at') or '(running)'}")
    print(f"\nLast refreshed: {ctx.cache.get_meta('last_refreshed')}")
    return 0


def cmd_install(args) -> int:
    src = Path(__file__).parent / "systemd" / "crowdsky-bot.service"
    dest_dir = Path.home() / ".config" / "systemd" / "user"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "crowdsky-bot.service"
    shutil.copyfile(src, dest)
    print(f"Installed user unit -> {dest}")
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "crowdsky-bot"]):
        try:
            subprocess.run(cmd, check=True)
            print(f"  ran: {' '.join(cmd)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  (please run manually) {' '.join(cmd)}  [{exc}]")
    print("Enable lingering so it starts at boot without login:")
    print("  sudo loginctl enable-linger $USER")
    return 0


def cmd_uninstall(args) -> int:
    for cmd in (["systemctl", "--user", "disable", "--now", "crowdsky-bot"],):
        try:
            subprocess.run(cmd, check=False)
        except Exception:  # noqa: BLE001
            pass
    unit = Path.home() / ".config" / "systemd" / "user" / "crowdsky-bot.service"
    if unit.exists():
        unit.unlink()
        print(f"Removed {unit}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crowdsky-bot")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-c", "--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the web UI + scheduler daemon")
    ro = sub.add_parser("run-once", help="run one nightly pass (dry-run by default)")
    ro.add_argument("--execute", action="store_true",
                    help="actually stack + upload instead of dry-run")
    sub.add_parser("status", help="print cached scopes and recent runs")
    sub.add_parser("install", help="install + enable the systemd user unit")
    sub.add_parser("uninstall", help="remove the systemd user unit")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    return {
        "serve": cmd_serve,
        "run-once": cmd_run_once,
        "status": cmd_status,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
