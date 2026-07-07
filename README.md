# crowdsky_bot

The goal of this repo is to make a small seestar add-on service that enables a user to automatically contribute frames to the crowdsky service (https://crowdsky.univie.ac.at)
The proposed hardware is a raspi zero W (1st gen) that is plugged in and connects automatically to a user's home wifi. 
The user's seestar(s) should also be connected to that wifi, and should be visible at seestar.local (or seestar-N.local for multiple). However the seestarpy package should take care of the connections, assuming everything is on the local wifi network.
The user should already have an account on the crowdsky service.

## Main reason for being for this code-base/service
This "crowdsky bot" should, if desired by the user, at dawn check all seestars on the network for new raw files. 
If they exist, it should trigger the seestar to start stacking them into 15-min chunks (conditional on a min 240s expousre time in that time chunk window).
If multiple seestars on the network, it should trigger in parallel the 15-min stacks, as these generally take ~1-2 mins to complete (depending on number of raws to stack)
Once all stacking is completed on the seestars on-board computer, this crowdsky-bot should retrieve the list of stacks already on the crowdsky server, cross-check this against what "crowdsky*.fit" files are waiting on the seestars, then transfer any missing chunk-stacks up to the crowdsky server using the user's crowdsky credentials. 

## Needed infrastructure
- A web interface that allows the user to: 
  - set the options regarding crowdsky: credentials, preferences for auto-stacking/upload. These options should persist beyond reboot of the raspi zero, so that the user does not need to set up things again if they unplug and replug the crowdsky-bot somewhere else. 
  - lists a summary table of uploaded/to-be-uploaded/to-be-stacked "chunk frames"
  - Offers a gallery view of the pngs available on the seestar for a selected target
  - See D:\Repos\crowdsky_bot\screenshots\Screenshot 2026-07-02 124520.png for my current idea on the web interface design - though this is not set in stone. Feel free to suggest improvements

- A persistent service that starts on boot which:
  - waits for dawn at the users location, then loads the config files for all seestar for which a config file exists
  - if desired, stacks on the seestar and uploads any new chunk frames from the previous nights observations
  - if no new observations, still checks that all crowdsky*.fits files on the seestar have been uploaded to
  - serves the above mentioned web interface

## Deliverables for version 1
- A python package that I can (uv add) pip install git+http.... on the raspi zero that sets up the service daemon and runs the web interface

## Installation & usage (v1)

See `DESIGN.md` for the full architecture. The `crowdsky_bot` package is a thin
appliance around `seestarpy.crowdsky`: a dawn scheduler, a single-worker job
queue, a persistent TOML config, a SQLite status cache/audit log, and a Flask
web UI.

Install into the Pi's uv project (keeps pulling prebuilt armv6 wheels via the
piwheels index pinned in `pyproject.toml` — see `SEESTARPY_SETUP.md`):

```bash
cd ~/crowdsky_service
uv add git+https://github.com/astronomyk/crowdsky_bot
```

CLI:

```bash
crowdsky-bot serve        # run the web UI + dawn scheduler daemon
crowdsky-bot run-once     # exercise one nightly pass (dry-run; --execute to run)
crowdsky-bot status       # print cached scopes + recent runs
crowdsky-bot install      # install + enable the systemd user unit
```

- Web UI: `http://<pi>:8080` (config, manual triggers, status table, gallery).
- Config: `~/.config/crowdsky_bot/config.toml` (mode 600; holds the CrowdSky
  password). State DB alongside it as `state.db`.
- Default trigger: `sunrise - 30 min`, auto stack + upload, all targets, local
  stack archive under `~/crowdsky_stacks` with a `1 GB × n_scopes` free-space
  retention floor. All editable in the UI or the TOML file.

Location and timezone are read automatically from the Seestar (the Pi's OS
timezone is set to match, which is required for correct CrowdSky chunk keys).

## Access to the current raspi zero
(not to be committed to github) -> see D:\Repos\crowdsky_bot\testing_local_raspi_zero.txt

## Open architectural point for later
Currently we assume that the user is capable of setting up their own raspi zero W.
However I want to be able to offer to send a production-ready raspi to anyone.
There are two open questions for later:
- how can a new user connect to the web interface if they are not capable of giving the raspi wifi access on their own. (pOtential solution, the raspi creates it's own hotspot, and the user adds their local network credentials to the settings page. Or we make use of the bluetooth functionality of the raspi zero.) 
- how can a new user use this bot if they have not connected their seestar in station mode to a local wifi network, and continue to only access their seestar via the seestars on-board hotspot AP. (Potential solution, the user provides the SSID and password of the seestar's AP, then at dawn (or when the user clicks cycle) the raspi disconnects from its current connects, looks for the seestar AP, coinnects to that, triggers all the stacks, downloads those stacks to the raspi SD card, then reconnects to the local wifi for uploading and communicating with the user.)

## Extra info
seestarpy dev version: E:\WHOPA\seestarpy
crowdsky dev version: D:\Repos\CrowdSky


