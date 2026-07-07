# seestarpy on the crowdsky bot (Raspberry Pi Zero W)

This document records how [`seestarpy`](https://github.com/astronomyk/seestarpy)
was installed and configured on the Raspberry Pi that runs the CrowdSky
auto-stack-and-upload service, what parts of seestarpy this service relies on,
and where to find the seestarpy source on the local dev machine.

Written 2026-07-02.

---

## 1. Target hardware

| | |
|---|---|
| Host | `crowdskybot` (SSH: `ingo@crowdskybot`) |
| Model | Raspberry Pi Zero W Rev 1.1 |
| Arch | `armv6l` — single-core ARM11, ~426 MB RAM |
| OS | Raspbian GNU/Linux 13 (trixie) / Debian 13 |
| Python | system CPython 3.13.5 (`/usr/bin/python3.13`) |
| Service dir | `~/crowdsky_service/` (a `uv` project) |
| Package mgr | `uv` 0.11.26 at `~/.local/bin/uv` (not on the default non-interactive PATH) |

The service does a nightly pass: once observing finishes, it stacks any new
sub-frames on the telescope and uploads the finished stacks to the CrowdSky
server.

---

## 2. Installing seestarpy (the armv6 problem and the fix)

### The problem

`uv add seestarpy` failed while trying to compile a C extension. The
dependency chain is:

```
seestarpy → cryptography → cffi
```

PyPI ships **no prebuilt wheels for `armv6l`** (manylinux does not target
ARMv6), so `uv` fell back to building from source:

- `cffi` failed immediately — missing `Python.h` (`python3-dev`) and
  `libffi-dev`.
- Even after that, `cryptography` (≥3.4) needs a **Rust toolchain** plus
  `libssl-dev`, and building a Rust extension on a single-core ARM11 with
  ~300 MB free RAM realistically OOMs or takes an hour-plus.

### The fix (piwheels)

[piwheels](https://www.piwheels.org) hosts prebuilt ARM wheels for exactly
these packages. We point `uv` at it, using `unsafe-best-match` so `uv`
actually consults piwheels instead of stopping at PyPI's source distribution
(uv's default `first-index` strategy would grab the PyPI sdist and never look
at piwheels):

```bash
export PATH="$HOME/.local/bin:$PATH"
cd ~/crowdsky_service
uv add seestarpy \
  --extra-index-url https://www.piwheels.org/simple \
  --index-strategy unsafe-best-match
```

`cryptography` and `cffi` then arrive as **prebuilt wheels — zero
compilation**.

### Making it reproducible

`--extra-index-url` is *not* persisted to `pyproject.toml`, so a future
`uv lock --upgrade` could silently revert to a source build. The piwheels
index and strategy are therefore pinned in `~/crowdsky_service/pyproject.toml`:

```toml
[tool.uv]
index-strategy = "unsafe-best-match"

[[tool.uv.index]]
name = "piwheels"
url = "https://www.piwheels.org/simple"
```

The `uv.lock` records `cryptography`'s source as
`https://www.piwheels.org/simple`, so `uv sync` on this Pi always reuses the
wheels.

> **Caveat:** piwheels wheels are tagged for the current Raspberry Pi OS
> Python (3.13 on trixie). If the OS Python major/minor is ever upgraded,
> re-run `uv lock` so it picks matching wheels.

---

## 3. The RSA signing key

Seestar firmware 7.18+ requires a mandatory RSA challenge-response handshake:
the client signs a challenge with a private RSA key (SHA-1, PKCS#1 v1.5).
`seestarpy`'s `auth.py` uses the `cryptography` library when present and falls
back to the `openssl` CLI otherwise.

seestarpy discovers the key in this order:

1. `$SEESTAR_KEY_PATH`
2. `./seestar.pem` (current working dir)
3. `~/.seestarpy/seestar.pem`  ← **default location used here**

The key was copied from the dev machine (`C:\Users\k_man\.seestarpy\seestar.pem`)
to the Pi and locked down:

```
/home/ingo/.seestarpy/seestar.pem      (mode 600)
/home/ingo/.seestarpy/                 (mode 700)
```

Verified: `python -c "from seestarpy import auth; print(auth.KEY_PATH)"`
resolves to `/home/ingo/.seestarpy/seestar.pem`, so no runtime configuration
is needed. This key is a secret — it is not committed to this repo and must
not be.

---

## 4. What seestarpy provides for the CrowdSky service

seestarpy is a full SDK for the ZWO Seestar S50/S30 (JSON-RPC on port 4700,
binary image stream on 4800/4804, SMB/HTTP file access). The bot only needs a
slice of it. The relevant modules:

### `seestarpy.crowdsky` — the end-to-end workflow

This subpackage is purpose-built for CrowdSky and drives the whole nightly
pass:

```python
from seestarpy import crowdsky

# 1. Stack: find every unstacked time-block for every target and batch-stack
#    it on the telescope. Blocks are grouped by time window (default 15 min)
#    and skipped below a minimum effective exposure (default 240 s).
crowdsky.stack_all(dry_run=True)          # preview
result = crowdsky.stack_all()             # do it

# 2. Upload: push the finished CrowdSky_*.fit stacks to the server,
#    skipping any chunk key already uploaded.
crowdsky.set_credentials("user", "pass")
crowdsky.upload_all_stacks(dry_run=True)  # preview
crowdsky.upload_all_stacks()              # do it
```

Useful building blocks also exported from `seestarpy.crowdsky`:

- `find_unstacked_blocks(target)` / `stack_blocks(target)` — per-target stacking
- `list_targets()` — observation folders on the scope
- `list_stacks()` / `upload_stack()` / `download_stack()` — server API
- `compute_chunk_key()`, `parse_light_filename()` — chunk-key / filename logic
- `set_base_url()` — point at a non-default CrowdSky server

### `seestarpy.stack` — low-level batch stacking

`get/set_batch_stack_setting`, `start_batch_stack`, `stop_batch_stack`,
`get_batch_stack_status`, `clear_batch_stack` — the firmware batch-stack
primitives that `crowdsky.stack_blocks` builds on. (Note: `set_batch_stack_setting`
is firmware-aware; v7.75 requires full paths.)

### `seestarpy.data` — FITS file access

`list_folders`, `list_folder_contents`, `download_file`, `download_folder`,
`delete_files` — SMB/HTTP access to the FITS files on the Seestar's storage.

### `seestarpy.connection` / `seestarpy.raw` / `seestarpy.ui`

Core TCP/JSON-RPC transport (`send_command`, `DEFAULT_IP` auto-discovered via
mDNS), the 59 low-level RPC wrappers, and high-level convenience functions
(`open`, `close`, `goto`) if the bot ever needs to command the scope directly.

---

## 5. Where to find seestarpy on the local machine

- **Dev checkout (source of truth for changes):** `E:\WHOPA\seestarpy`
  (on this Windows dev box)
- **GitHub:** https://github.com/astronomyk/seestarpy
- **PyPI:** `pip install seestarpy` (currently v0.5.0)
- **Docs:** https://seestarpy.readthedocs.io

This CrowdSky bot repo lives at `D:\Repos\crowdsky_bot`
(https://github.com/astronomyk/crowdsky_bot).

---

## 6. Known follow-up (next seestarpy release)

The piwheels dependency is a workaround. The proper fix is to make
`cryptography` an **optional** dependency in seestarpy rather than a hard one —
`auth.py` already falls back to the `openssl` CLI (present on the Pi), so the
RSA handshake works without the Python package. With that change, armv6 /
low-power installs would need **zero compiled dependencies** and would not
depend on piwheels at all. This is planned for the next release (0.5.1),
alongside other queued changes and testing against the Seestar firmware update
expected mid-July 2026.
