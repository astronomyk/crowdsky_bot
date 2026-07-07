# CrowdSky Bot — Design / Implementation Spec

Status: draft for v1. Written 2026-07-02. This is the spec to implement from.

## 0. Guiding principle

`seestarpy.crowdsky` is the engine. It already does the full nightly pass,
idempotently, and fans out across scopes in parallel (`@multiple_ips`), while
stacking sequentially *within* a scope (correct — the firmware batch-stacks one
job at a time). Heavy compute (stacking) runs **on the Seestar**, not the Pi.

Therefore **this repo is a small appliance around that library**: a daemon, a
dawn scheduler, a persistent config store, a SQLite status cache + audit log,
and a lightweight web UI. Keep that scope discipline — no astronomy logic lives
here that seestarpy already owns.

Target host: Raspberry Pi Zero W (armv6l, single core, ~300 MB free RAM),
Raspbian trixie, system CPython 3.13, `uv` project at `~/crowdsky_service`.

## 1. Hard constraints → tech choices

- **No new compiled dependencies.** armv6 has no manylinux wheels; anything with
  Rust/C (pydantic-core, uvicorn deps) reopens the piwheels pain in
  SEESTARPY_SETUP.md. Everything below is pure Python.
- **Web:** Flask + Jinja2 + waitress (WSGI). All pure Python. The real work is
  blocking and offloaded to a worker thread, so async (FastAPI/uvicorn) buys
  nothing and costs wheels.
- **Scheduler:** `astral` (pure Python) for twilight/sunrise; a plain scheduler
  thread that sleeps until the next trigger. No APScheduler needed.
- **State:** `sqlite3` (stdlib) for the status cache + audit log.
- **Config:** TOML file on the SD card (`tomllib` to read, `tomli-w` to write —
  tomli-w is pure Python; or hand-serialise to avoid the dep).
- **Concurrency:** one process, multiple threads. seestarpy work is I/O-bound
  (socket polling, SMB, HTTP) so the GIL is released; threads are cheap on RAM.
  **No multiprocessing** (fork cost + memory on a Pi Zero).
- Existing deps already present via seestarpy: `requests`, `tzlocal`, `pysmb`.

## 2. Package layout

```
crowdsky_bot/
  __init__.py
  config.py        # TOML load/save, defaults, mode-600, path resolution
  scopes.py        # discovery (find_available_ips), per-scope metadata,
                   #   location + timezone sync from the Seestar
  cache.py         # SQLite: schema, status-cache upsert, audit log, queries
  jobs.py          # thin structured wrappers over crowdsky.* with progress hooks
  worker.py        # single-worker job queue + lock + live progress state
  scheduler.py     # astral dawn calc -> enqueue nightly pass; boot catch-up
  service.py       # app assembly + thread startup; graceful shutdown
  web/
    app.py         # Flask: page + JSON endpoints + thumbnail proxy
    templates/index.html
    static/app.js, style.css
  cli.py           # `crowdsky-bot serve | install | uninstall | status | run-once`
  systemd/crowdsky-bot.service   # unit template
```

Entry point (pyproject): `crowdsky-bot = "crowdsky_bot.cli:main"`.

## 3. Config schema (TOML)

Location: `$CROWDSKY_BOT_CONFIG` else `~/.config/crowdsky_bot/config.toml`.
File mode `600`, dir `700` (it holds the CrowdSky password). Created with
defaults on first run.

```toml
[crowdsky]
username = ""
password = ""
base_url = "https://crowdsky.univie.ac.at"   # -> crowdsky.set_base_url()

[scopes]
count = 0            # 0 = auto-discover (probe seestar.local, seestar-2..N)
max_probe = 8        # how far to probe when auto-discovering
names = {}           # optional {ip = "friendly name"} for display

[location]
source = "seestar"   # "seestar" (auto) | "manual"
lat = 0.0            # used when source = "manual"
lon = 0.0
timezone = ""        # IANA; empty => read from scope / keep Pi default

[schedule]
auto_stack = true
auto_upload = true
trigger = "sunrise"      # "sunrise" | "astro_dawn" | "fixed"
offset_minutes = -30     # sunrise - 30 min (Seestar observes until sunrise-45)
fixed_time = "09:00"     # local HH:MM when trigger = "fixed"
catch_up_on_boot = true  # if today's trigger already passed and no run today

[stacking]
block_minutes = 15
min_exptime = 240
exclude_patterns = []    # process every folder; user may add fnmatch patterns

[storage]
stacks_dir = "~/crowdsky_stacks"   # persistent local archive of uploaded stacks
reserve_gb_per_scope = 1.0         # keep this much SD free per scope (~2 nights)
# Retention policy (see §5): stacks are KEPT locally after upload so the user
# can browse them offline and to spread SD wear. When free space on the
# stacks_dir filesystem drops below reserve_gb_per_scope * n_scopes, the OLDEST
# stacks are pruned to make room for the newest.

[web]
host = "0.0.0.0"
port = 8080
auth = "none"        # "none" | "pin"
pin = ""             # set when auth = "pin"
```

## 4. Startup sequence (`service.py`)

1. Load config (create defaults if missing).
2. `crowdsky.set_base_url(cfg.crowdsky.base_url)`; if creds present,
   `crowdsky.set_credentials(...)` and also export `CROWDSKY_USERNAME/PASSWORD`.
3. Discover scopes (`scopes.discover()`): `connection.find_available_ips(N)`
   where N = `scopes.count` or `max_probe` for auto. Persist scope metadata
   (ip, hostname, firmware via `get_device_state(["device"])`) to cache.
4. **Location + timezone sync** (`scopes.sync_location()`):
   - source="seestar": `raw.get_device_state(["location_lon_lat"], ips=<first live scope>)`
     -> `[lon, lat]`. **Note the [lon, lat] order.** Fall back to
     `raw.get_user_location()`.
   - Determine timezone: prefer the scope's tz (from `pi_get_time`, if it
     reports one) else config `timezone` else current Pi tz.
   - **Set the Pi OS timezone to match the site** via `timedatectl set-timezone`
     (best-effort; needs privilege — see §9). This is required for correct
     chunk keys, because `crowdsky.local_dt_to_chunk_str` interprets the
     Seestar's filename timestamps in the Pi's `get_localzone()`.
   - source="manual": use configured lat/lon/timezone.
5. Set `connection.VERBOSE_LEVEL = 0` (silence the library's stdout chatter).
6. Start the **worker** thread and the **scheduler** thread.
7. Enqueue an initial `refresh` job so the UI has data.
8. Start waitress serving the Flask app (blocks in the main thread).

Guard: don't run any scheduled pass until the clock is sane (NTP synced —
`timedatectl show -p NTPSynchronized`). Pi Zero has no RTC.

## 5. Worker + jobs

### Worker (`worker.py`)
- A single background thread consuming a `queue.Queue` of jobs. A
  `threading.Lock`/single-consumer guarantees **only one job runs at a time**,
  so the dawn trigger and any number of browser clicks can never run overlapping
  `stack`/`upload`.
- Holds a `Progress` object (thread-safe) the web layer reads via `/api/status`:
  `{state: idle|running, kind, started_at, message, per_scope: {ip: "M 81 block 3/7"}, last_run: {...}}`.
- Job kinds: `refresh`, `stack`, `upload`, `purge`. Each job carries a scope
  selection (list of IPs or "all") and, for stack/upload/purge, a target
  selection (list or "all"). De-dupe: if an identical job kind is already queued,
  collapse it.
- Every job writes a `runs` row (start, finish, status, summary JSON,
  triggered_by).

### Jobs (`jobs.py`) — structured wrappers with progress
Rather than call `crowdsky.stack_all()` as one opaque blob, the worker drives it
per scope so we get live progress and can update the cache incrementally.

- `refresh(scopes, targets)`:
  - `crowdsky.list_targets(ips=scope)` per scope.
  - For each (scope, target): count `CrowdSky_*.fit` on the scope
    (`data.list_folder_contents(target, filetype="fit")`, filter `CrowdSky_`),
    `find_unstacked_blocks(target, ips=scope)` filtered by `min_exptime` for the
    "awaiting stacking" count.
  - Once per refresh: `crowdsky.list_stacks()` (server) grouped by `object_name`
    -> "on server" counts and the set of uploaded `chunk_key`s.
  - "awaiting upload" = local `CrowdSky_*` chunk keys not in the server set.
  - Upsert all rows into `targets` cache with `updated_at`.
  - Apply `exclude_patterns` (fnmatch) so calibration/Unknown folders don't clutter.
- `stack(scopes, targets)`: for each scope (in parallel across scopes via a
  `ThreadPoolExecutor(max_workers=n_scopes)`, each thread updates its own
  `per_scope` progress line), loop targets and call
  `crowdsky.stack_blocks(target, ips=scope, block_minutes=..., min_exptime=...)`.
  **Before stacking a scope, check `raw.get_view_state(ips=scope)`; if it is
  actively exposing (`View.stage`/`state` == working/exposing), skip that scope
  this pass** (it's still observing). Aggregate per-target summaries.
- `upload(scopes, targets)`: `crowdsky.upload_all_stacks(target=..., ips=scope,
  dest=<stacks_dir>/<scope>, skip_existing=True)`. Files are **kept** as a local
  archive. Log each upload to the `uploads` audit table. After the upload job
  completes, call `storage.enforce_retention()` (§5.1).
- `purge(scopes, targets)`: `crowdsky.purge_crowdsky_stacks(folder=target, ips=scope)`.
  Deletes `CrowdSky_*` from the scope. (Destructive — UI must confirm.)
- After any stack/upload/purge job, enqueue a `refresh` so the table reflects
  reality.

Retry: upload failures are logged; the next dawn pass retries automatically
(idempotent). No in-run retry loop for v1 beyond seestarpy's own reconnect.

### 5.1 Local stack archive + retention (`storage` in `cache.py` or its own module)

Uploaded stacks are kept under `stacks_dir/<scope>/<target>/CrowdSky_*.fit` as a
persistent archive. Rationale (Kieran): lets the user browse their stacks even
when the Seestars are offline, and spreads SD write wear (fewer delete/rewrite
cycles than download-then-delete).

`enforce_retention()`:
- Compute `reserve = reserve_gb_per_scope * n_scopes` (bytes).
- Read free space on the `stacks_dir` filesystem (`shutil.disk_usage`).
- While `free < reserve`: find the **oldest** `CrowdSky_*.fit` in `stacks_dir`
  (order by the observation timestamp in the chunk key, tie-break on mtime),
  delete it (and its `.jpg`/`_thn.jpg` companions if present), recompute free.
- Stop if the dir is empty (log a warning — reserve can't be met; something else
  is filling the card).
- Log a `runs`-style summary of what was pruned. Runs after every upload job and
  is cheap enough to also run at the start of one.

Note: the archive holds FITS (+ any downloaded companions). The offline gallery
(§7) reads jpgs — for a fully offline gallery we'd also cache `_thn.jpg`; for v1
the gallery proxies thumbnails from the scope when online and falls back to any
locally-archived jpgs. Keep this simple in v1.

## 6. Scheduler (`scheduler.py`)

- Compute the next trigger datetime from `[location]` + `[schedule]`:
  - `sunrise`: `astral` sunrise + `offset_minutes` (default -30). **Chosen
    default.** The Seestar observes only within sunset+45 min .. sunrise-45 min,
    so sunrise-30 is 15 min after imaging has definitely stopped. Also the most
    robust option: sunrise always exists, whereas astronomical dawn is undefined
    at high latitudes in summer (sun never reaches -18°), which would raise in
    astral and needs a fallback.
  - `astro_dawn`: `astral` dawn for `depression=18` (astronomical) + offset
    (Kieran noted +60 min is also acceptable). If astral reports no dawn for the
    date (polar summer), fall back to `sunrise + offset_minutes`.
  - `fixed`: today/tomorrow at `fixed_time` local.
- Sleep until then (wake early enough to re-read config if it changed). On fire:
  if `auto_stack`, enqueue `stack(all, all)`; then if `auto_upload`, enqueue
  `upload(all, all)` (worker runs them in order). Record `triggered_by="auto"`.
- **Boot catch-up:** on startup, if `catch_up_on_boot` and today's trigger time
  has already passed and there is no successful auto `runs` row for today, fire
  once now (covers unplug/replug during the day).
- Never fire before NTP is synced (see §4 guard); if not synced, poll and defer.

## 7. Web layer (`web/app.py`)

Server-rendered shell (`index.html`) + a small `app.js` that polls JSON. No
build step, no CDN (works offline on the LAN).

### Pages
- `GET /` — the single page: Setup & Credentials, Schedule/Stacks (toggles +
  manual buttons + summary table), View Stacks (gallery). Reflects the mockup
  in `screenshots/`, with the changes in §8.

### JSON API
- `GET  /api/status`   -> worker Progress + last run.
- `GET  /api/summary`  -> cached table rows `[{scope, scope_name, target,
                          on_server, awaiting_upload, awaiting_stacking}]` +
                          `last_refreshed`.
- `POST /api/refresh`  -> enqueue refresh.
- `POST /api/stack`    -> body `{scopes:["all"], targets:["M 81",...]|["all"]}`.
- `POST /api/upload`   -> same body shape.
- `POST /api/purge`    -> same body shape (requires confirm flag).
- `GET  /api/config`   -> current config (password redacted).
- `POST /api/config`   -> validate + save; re-run discovery/location sync if
                          scope/location fields changed.
- `GET  /api/gallery?scope=<ip>&target=<t>` -> list of thumbnail proxy URLs
                          (`data.list_folder_contents(target, filetype="thn.jpg")`).
- `GET  /proxy/<scope_ip>/<path:p>` -> **stream** a file from the scope's own
                          HTTP server (`http://<ip>/<p>`). Used for gallery
                          thumbnails/jpgs so nothing is copied to the SD card.
                          Validate `scope_ip` against known scopes; only allow
                          paths under `MyWorks/`.
- `GET  /api/runs`     -> recent audit-log rows.

### Auth
`web.auth = "none"` (LAN trust) or `"pin"` (a shared PIN set on first run,
checked via a signed cookie / simple session). Default none for v1; PIN is the
hook for the future "mail a unit" scenario.

## 8. UI changes vs the mockup

- **Drop the "active Seestar" radio buttons.** The nightly job hits all scopes in
  parallel; the same target can exist on two scopes (observed: `FR Camelopardalis`
  on two of four test scopes). Instead add a **Scope column** to the table. Keep
  "number of Seestars" as an int that feeds discovery, but default it to
  auto-discover (0).
- **Add a status/activity strip** near the trigger buttons: current job + live
  per-scope progress + last-run result + recent failures (from `/api/status`,
  `/api/runs`).
- **Add Location + Timezone fields** to Setup (auto-filled from the scope,
  editable). These were absent from the mockup and are load-bearing (§0 gotchas).
- **Gallery proxies thumbnails** from the scope (no SD copies) and hides folders
  matching `exclude_patterns`.
- Per-row checkboxes select which targets the manual buttons act on; auto mode
  always acts on all (minus excludes).

## 9. Packaging, service, privileges

- Installable via `uv add git+https://github.com/astronomyk/crowdsky_bot` (keep
  the piwheels index pin from SEESTARPY_SETUP.md in the consuming project).
- `crowdsky-bot install` writes and enables a systemd **user** or **system**
  unit (`systemd/crowdsky-bot.service`) running `crowdsky-bot serve`, with
  `Restart=on-failure`, `Environment=PATH=%h/.local/bin:...`, WorkingDirectory
  `~/crowdsky_service`.
- Setting the Pi timezone (`timedatectl set-timezone`) needs root. Options:
  run the service as a system unit, or ship a tiny sudoers rule limited to
  `timedatectl set-timezone`. If privilege is unavailable, fall back to setting
  the process env `TZ` for the running app so `tzlocal.get_localzone()` used by
  seestarpy resolves correctly (verify this is honoured), and warn in the UI.
- The RSA `seestar.pem` stays at `~/.seestarpy/seestar.pem` (already set up).

## 10. Data model (SQLite)

```sql
scopes(ip TEXT PK, hostname TEXT, name TEXT, firmware TEXT,
       lon REAL, lat REAL, last_seen TEXT)

targets(scope_ip TEXT, target TEXT,
        on_server INT, awaiting_upload INT, awaiting_stacking INT,
        updated_at TEXT, PRIMARY KEY(scope_ip, target))

runs(id INTEGER PK, kind TEXT, triggered_by TEXT,
     started_at TEXT, finished_at TEXT, status TEXT, summary_json TEXT)

uploads(chunk_key TEXT, object_name TEXT, scope_ip TEXT, filename TEXT,
        uploaded_at TEXT, status TEXT)
```
Server (`list_stacks`) is the source of truth for "already uploaded"; the
`uploads` table is a local audit log + fast lookup, not authoritative.

## 11. Decisions (settled 2026-07-02)

1. **Dawn trigger** — `sunrise - 30 min` (`trigger="sunrise"`,
   `offset_minutes=-30`). Observing window is sunset+45 .. sunrise-45, so this
   fires 15 min after imaging stops. `astro_dawn + 60 min` is an acceptable
   alternative (with a sunrise fallback for polar summer).
2. **Target filtering** — **process everything** (`exclude_patterns=[]`). The
   CrowdSky server rejects anything unwanted; users can add patterns in the UI.
3. **Web auth (v1)** — **none**, trust the LAN. PIN reserved for the mail-out
   phase.
4. **SD hygiene** — **keep local stacks as an archive** with a free-space
   retention policy (§5.1): keep newest, prune oldest when free space drops
   below `reserve_gb_per_scope * n_scopes` (default 1 GB/scope ≈ 2 nights).
   Enables offline browsing and reduces SD write wear.

## 12. Build phases

1. `config` + `scopes` (discovery + location/tz sync) + `cache` + `jobs`
   wrappers + `cli run-once` (dry-run nightly pass). Validate on the 4 test
   scopes with `dry_run=True` end-to-end (server half needs real creds).
2. `worker` + `scheduler` + `service` + systemd. The actual daemon.
3. Flask read-only status page off the cache (`/`, `/api/status`, `/api/summary`).
4. Manual triggers + config editing + gallery proxy.
5. Hardening for mail-out: hotspot/BLE onboarding, PIN auth, seestar-AP fallback
   (the README "open architectural points").
```
