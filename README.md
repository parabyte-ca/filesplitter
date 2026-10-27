# FileSplitter

**Version 1.2.6**

A self-hosted Docker service for TrueNAS (or any Linux host) that automatically indexes your media library, re-encodes video files to x265, and splits multi-scene anthology files at scene boundaries.

---

## Features

- **Auto-scan** — walks your media paths and classifies files by codec, duration, size, and keyword
- **x265 encoding** — re-encodes to HEVC with configurable CRF and preset; progress is streamed live
- **Scene splitting** — detects scene changes with ffmpeg and stream-copies each scene to its own file
- **Anthology detection** — flags files by duration (≥1 hr), size (≥4 GB), or keyword (Vol, Compilation, …)
- **Live dashboard** — dark-themed UI with SSE-driven progress, file table, job queue, and settings
- **Persistent state** — SQLite database with WAL mode survives restarts with no data loss
- **Docker-first** — single `docker compose up -d --build` deploys everything on port 4250
- **In-app updates** — checks its own GitHub repo daily for a newer release and can self-upgrade from the dashboard via an optional host helper script
- **Trash with one-click empty** — deleted files move to a `.trash` folder and auto-purge after 30 days, or empty immediately from the Settings tab

---

## Requirements

- Docker with Compose (v2 plugin recommended)
- Git
- Media volumes accessible to the Docker host

---

## Installation

```bash
git clone https://github.com/parabyte-ca/filesplitter.git
cd filesplitter
./install.sh
```

The installer checks dependencies, builds the image, and starts the service. Open the dashboard at `http://<host-ip>:4250`.

---

## Updating

```bash
./update.sh
```

This pulls the latest code, rebuilds the image, and restarts the container with zero manual steps. It also backs up the database, builds the new image, tests it against a copy of the database before touching the running container, and automatically rolls back if the new version doesn't come up healthy.

---

## Updates

The dashboard's Settings tab shows whether a newer release of FileSplitter is available, with
its release notes, and can trigger an upgrade for you — no SSH session needed.

### How it works

- Once a day (and whenever you click **Check now**, rate-limited to once a minute), the
  container checks `APP_SOURCE_URL`'s GitHub releases for a newer version. Nothing about your
  server is ever sent — it's a plain unauthenticated read of the public releases API.
- Set `APP_UPDATE_CHECK=false` to disable the daily check entirely (the dashboard still works,
  it just won't look for updates).
- The app container never gets Docker or root access, by design. When you click **Upgrade now**,
  it writes a small request file into the shared `./upgrade` folder — that's the *only* channel
  between the container and the host. A small script running as root on the host,
  `scripts/upgrade-agent.sh`, picks up the request (on its own schedule, typically every 15
  minutes) and is the only thing that actually runs `./update.sh`.
- Everything the helper finds in `./upgrade` is treated as untrusted input: fields are
  validated with strict patterns, symlinks are refused outright, and only a validated version
  string is ever passed to `./update.sh` — never arbitrary commands. The folder is `chmod 770`
  for basic hygiene, but since the app container runs as root inside its own container (a
  deliberate choice, consistent with Sonarr/Radarr/Plex-style containers), that mode is not the
  real security boundary — the real boundary is that the container has no Docker socket, no
  `--privileged` flag, and no other way to reach the host at all, and the helper never trusts or
  acts on anything in the folder beyond those few validated fields.

### Host helper setup

In-app upgrades require the helper to be scheduled on the host once:

```bash
scripts/upgrade-agent.sh --install            # every 15 minutes (default)
scripts/upgrade-agent.sh --install --every 30 # or pick another interval
```

This prefers `/etc/cron.d` if writable, falling back to a systemd timer. On a host with its own
managed scheduler (e.g. a NAS web UI), use `--print` instead to get the cron line or systemd
unit text to paste in yourself, without this script touching `/etc`:

```bash
scripts/upgrade-agent.sh --print
```

Run `scripts/upgrade-agent.sh --uninstall` to remove the schedule (your upgrade history and
`upgrade-agent.env` config are left in place). Until the helper is installed and has run at
least once, the dashboard shows "Helper: not configured" and the Upgrade button stays hidden —
you can still see that an update is available, you just can't trigger it from the app yet.

To turn off app-triggered upgrades entirely, simply never run `--install` (or run `--uninstall`)
— the terminal-based `./update.sh` workflow above is unaffected either way.

---

## GPU Acceleration (NVENC)

If your TrueNAS server has an NVIDIA GPU, you can use hardware-accelerated HEVC encoding via NVENC — typically **5–10× faster** than CPU:

| Encoder | 1080p speed | 1-hr 4K encode |
|---|---|---|
| CPU (libx265 medium) | 30–80 fps | 1–4 hours |
| GPU (hevc_nvenc) | 200–500 fps | 10–20 min |

**Trade-off**: NVENC quality at the same numeric value is slightly below libx265, but visually near-indistinguishable. Use CQ 22–24 instead of CRF 28 for comparable quality.

### Prerequisites

1. NVIDIA drivers installed on the TrueNAS host
2. `nvidia-container-toolkit` installed (`apt install nvidia-container-toolkit` then restart Docker)

### Enable GPU encoding

1. Edit `docker-compose.yml` — uncomment both the `ENCODER_BACKEND: nvenc` line and the `deploy` block at the bottom
2. Run `./update.sh`
3. Open the dashboard → Settings tab → the **GPU detected ✓** badge should appear green
4. Select **GPU (NVENC)** as the backend and save

The Settings tab will show the available NVENC presets and update the quality label to **CQ** (equivalent scale to CRF, same 0–51 range). If NVENC is unavailable at runtime, jobs fall back to CPU automatically with a log warning.

**GPU detection at startup**: the container actually attempts a real NVENC encode at boot to confirm the GPU is usable, rather than just checking that ffmpeg supports it. If a GPU is configured (`/dev/nvidia*` devices present) but the driver is still initializing right after a host reboot, the container retries every 5 seconds for up to `NVENC_PROBE_MAX_WAIT` seconds (default 120s) before giving up for that run — you'll see `"GPU device present but hevc_nvenc probe failed — driver may still be initializing"` in the logs during this window, which is expected and not an error. Installs without a GPU configured see no added startup delay.

**If the driver is still slower than that**: the container keeps retrying every 5 minutes in the background even after giving up at boot, so it will pick the GPU up on its own without a restart — or click **Recheck GPU** next to the badge in Settings for an immediate on-demand check. For a permanent fix, enable `nvidia-persistenced` on the host (`nvidia-smi -pm 1`, or the `nvidia-persistenced` systemd service) — it keeps the driver initialized across container restarts so there's no cold-start race to begin with.

---

## Configuration

All settings are environment variables in `docker-compose.yml`. The dashboard Settings tab lets you change encoding and scene-detection parameters at runtime without a restart.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_PATHS` | `/media` | Comma-separated container paths to scan |
| `FLASK_PORT` | `4250` | Dashboard port |
| `SCENE_THRESHOLD` | `0.4` | Scene detection sensitivity (0=everything, 1=nothing) |
| `MIN_SCENE_DURATION` | `120` | Minimum seconds between split points |
| `SPLIT_MIN_DURATION` | `5400` | Flag as anthology if duration ≥ this (seconds, default 90 min) |
| `SPLIT_MIN_SIZE` | `2147483648` | Flag as anthology if size ≥ this (bytes, default 2 GB) |
| `SPLIT_KEYWORDS` | `Vol,Compilation,…` | Filename keywords that trigger anthology classification |
| `ENCODER_BACKEND` | `cpu` | `cpu` (libx265) or `nvenc` (GPU HEVC — see GPU section above) |
| `NVENC_PROBE_MAX_WAIT` | `120` | Seconds to keep retrying the GPU probe at startup when a GPU is configured but not yet ready (see GPU section above) |
| `X265_CRF` | `28` | x265 CRF / NVENC CQ quality (18=high, 28=good, 35=small) |
| `X265_PRESET` | `medium` | Encoding speed preset (CPU: ultrafast→veryslow; NVENC: fast/medium/slow/hq/hp) |
| `TARGET_RESOLUTION` | `original` | Output resolution: `original`, `1080p`, `720p`, etc. |
| `MAX_WORKERS` | `2` | Simultaneous ffmpeg jobs |
| `FFPROBE_TIMEOUT` | `60` | Seconds to wait for ffprobe before giving up on a file (also adjustable from Settings → ffprobe Timeout) |
| `APP_SOURCE_URL` | `https://github.com/parabyte-ca/filesplitter` | GitHub repo checked for new releases — see **Updates** above |
| `APP_UPDATE_CHECK` | `true` | Set to `false` to disable the daily release check |
| `APP_UPGRADE_DIR` | `/upgrade` | Container path where the shared `./upgrade` folder is mounted — do not change without also changing the volume mount |

### Adding media paths

```yaml
# docker-compose.yml
volumes:
  - /mnt/shared_vol/adult/Bellesa:/media/bellesa
  - /mnt/shared_vol/adult/OtherStudio:/media/other   # add this

environment:
  MEDIA_PATHS: /media/bellesa,/media/other            # and this
```

Then run `./update.sh`.

---

## Workflow

1. **Scan** — click *Scan Now* to probe all files in your media paths
2. **Review** — the file table shows codec, duration, size, and anthology classification
3. **Queue** — click *Encode* or *Split* per file, or use the bulk-queue toolbar buttons
4. **Monitor** — switch to the *Active Jobs* tab to watch live progress

---

## Data

The SQLite database is stored at `./data/filesplitter.db` on the host (mounted into the container). Backups are as simple as copying that file.

Deleted files are moved to a `.trash` folder inside each media path (not removed from disk), and are purged automatically after 30 days. The Settings tab's **Trash** card shows how much space is currently in `.trash` and has an **Empty Trash** button to purge everything immediately instead of waiting.

---

## Revision History

| Version | Date | Notes |
|---|---|---|
| **1.2.6** | 2026-10-27 | Fix in-app upgrades failing for any install with local `docker-compose.yml` edits (e.g. the GPU deploy block): `update.sh`'s `--version X.Y.Z` path — the only thing the in-app Upgrade button ever runs — never stashed local changes before `git checkout`, so it would refuse to run and the upgrade would fail every time. Now stashes before the checkout and restores after, matching the plain `./update.sh` path's existing behavior |
| **1.2.5** | 2026-10-27 | GPU detection could still get stuck on CPU past the 1.2.2 fix if a slow driver took longer than its 30s boot-time retry window. Raises `NVENC_PROBE_MAX_WAIT` default to 120s, adds a background retry every 5 minutes after boot gives up (self-heals without a restart), and adds a **Recheck GPU** button in Settings for an immediate on-demand check |
| **1.2.4** | 2026-10-26 | Fix wasted vertical space in Settings → Updates on mobile: the "Check daily for a newer release" checkbox had a `padding-top` meant to align it with the input beside it in the desktop 2-column layout, but that padding still applied after the grid collapses to 1 column on narrow screens, leaving a large empty gap above it. Now scoped to the desktop breakpoint only |
| **1.2.3** | 2026-10-25 | Fix `./update.sh` failing with "You are not currently on a branch" on a plain update (no `--version`): the in-app Upgrade button always checks out a release tag, leaving the repo in detached HEAD, and a bare `git pull` has no tracking branch there. The default update path now explicitly re-attaches to the default branch before pulling |
| **1.2.2** | 2026-10-25 | Fix GPU detection sometimes reverting to CPU encoding after a container restart: `encoder.py`'s NVENC check now actually attempts a real encode instead of only checking that ffmpeg was compiled with NVENC support (which is always true regardless of hardware), and retries for up to `NVENC_PROBE_MAX_WAIT` seconds (default 30s) when a GPU is configured but the driver is still initializing right after a host reboot — with zero added delay for installs without a GPU configured. Healthcheck `start_period` raised to 60s to accommodate the worst case |
| **1.2.1** | 2026-10-24 | Fix silent job failures: jobs that failed before ffmpeg even started (an unreadable/corrupt file) wrote nothing to the Jobs History log — `codec_detector.probe_with_error()` now surfaces ffprobe's actual reason (uses `-v error` instead of `-v quiet` so stderr isn't empty), every failure path in encoder/splitter/trimmer reports it, and `worker.py` now guarantees both the file's error message and the job's log are always populated on any failure. Also adds a Settings → ffprobe Timeout field, since a tight 60s default was a plausible cause for large files on a slow NAS |
| **1.2.0** | 2026-10-21 | Trash card in Settings: shows current `.trash` size/file count across all media paths and adds an Empty Trash button to purge everything immediately (`trash.empty_trash()`), independent of the existing 30-day automatic cleanup |
| **1.1.0** | 2026-10-16 | In-app update checking and self-upgrade: Settings tab shows when a newer GitHub release is available with its release notes; a daily background check (`APP_SOURCE_URL`, `APP_UPDATE_CHECK`); one-button upgrade writes a request to a shared `./upgrade` folder for a new root-owned host helper (`scripts/upgrade-agent.sh`) to pick up and run — the app container never gets Docker or root access; `update.sh` now backs up the database, pre-flight-tests the new image against a copy of it, and automatically rolls back on a failed health check |
| **1.0.0** | 2026-06-07 | Allow re-encoding HEVC files at a different resolution — HEVC files now show a Transcode button to upscale or downscale via the encode modal |
| **0.9.9** | 2026-05-29 | Comprehensive hardening: queue loop now survives DB exceptions; `duration_sec==0` guard prevents corrupt split boundaries and original deletion; encoder cancel fires immediately (watcher thread) not just while ffmpeg produces stdout; proc always reaped on exception; UI settings persist across container restarts; auth now uses RFC-1918 LAN IP check instead of spoofable CF header; login uses `hmac.compare_digest` + 1s delay; session cookie gets `SameSite=Lax`; `get_json` null guard; SSE `onerror` indicator and keepalive comments; `.dockerignore` added; resource limits + log rotation in compose; `update.sh` stashes local edits before `git pull`; non-root user in Dockerfile; ffprobe timeout configurable; README defaults corrected; GitHub Actions CI added |
| **0.9.8** | 2026-05-27 | Fix NVENC `-22 Invalid argument` error: replace `-rc vbr -cq` with `-cq -b:v 0` which is compatible across all NVENC driver/GPU versions |
| **0.9.7** | 2026-05-27 | Fix job cancellation: cancel_event is now registered in `_queue_loop` before `_executor.submit()` so queued-but-not-yet-running jobs can be cancelled immediately; encoder adds a stderr drain thread to prevent the 64 KB pipe buffer from deadlocking ffmpeg and stalling the cancel check |
| **0.9.6** | 2026-05-27 | Anthology classifier now skips files whose name contains an `SxxExx` episode pattern — large BluRay remuxes of TV episodes are no longer misclassified as anthologies |
| **0.9.5** | 2026-05-27 | Cloudflare Tunnel auth: `DASHBOARD_PASSWORD` env var gates access from CF Tunnel while LAN access remains open; session-based login with dark-themed form; Logout button in header; persistent pause state (survives container restarts via `settings` table); elapsed timer only shown for actively running jobs |
| **0.9.4** | 2026-05-27 | Persistent Space Saved counter: cumulative total stored in `settings` table — survives restarts and Clear History; Settings tab gains a Space Saved Counter section with manual set and Reset to 0; `init_db()` seeds the value from existing job rows on first upgrade |
| **0.9.3** | 2026-05-26 | Black-frame scene detection: add `blackdetect` filter support for fade-to-black anthology transitions; `SCENE_METHOD=auto` (default) tries frame-diff first then falls back to blackdetect; `select` and `black` modes also available; configurable via Settings tab and env vars |
| **0.9.2** | 2026-05-26 | Fix: codec not updated after encode — files stayed as h264 in the DB after x265 encoding, causing Encode button to reappear and incorrect codec display; `update_file_size` now writes `codec=hevc` on success; startup migration fixes existing affected records |
| **0.9.1** | 2026-05-26 | Backfill `saved_bytes` from existing `log_tail` strings on startup — historical encode savings are recovered automatically from the `"N GB → M GB"` text stored in prior job records |
| **0.9.0** | 2026-05-26 | Cumulative space-saved stat card (green, live via SSE): `saved_bytes` stored per encode job, summed in `get_stats()`; DB migration adds column to existing installs |
| **0.8.9** | 2026-05-26 | Encode modal shows original resolution (width × height) sourced from ffprobe; width/height stored in DB and populated on scan |
| **0.8.8** | 2026-05-26 | Mobile-friendly layout: responsive stats grid (3×2 on phones), horizontal table scroll, Codec/Duration columns hidden on small screens, header wraps cleanly; docker volume warning added to Settings → Scan Paths |
| **0.8.7** | 2026-05-26 | Encode savings display (% + sizes shown in Recent History after encode job); content-based scene naming via ThePornDB (per-scene pHash keyframe matching → title-parse fallback → numbered fallback); split output renamed to `"01 - Scene Name.mp4"` or `"{stem} 01.mp4"` |
| **0.8.6** | 2026-05-26 | Scene detection stall fix: stderr pipe now drained in a background thread, preventing ffmpeg from deadlocking on the 64KB pipe buffer; incremental progress reported 2–33% by parsing ffmpeg's time= output; job timer added to Active Jobs tab |
| **0.8.5** | 2026-05-26 | Anthology logic overhaul: keywords removed from classifier (too noisy); pure duration OR size logic with recalibrated defaults (90 min / 2 GB) — catches all multi-hour 720p compilations while leaving single short-form files unclassified |
| **0.8.4** | 2026-05-26 | Bug fixes: encode button broken for any filename (JSON.stringify double-quotes break onclick attribute — fixed by passing only file ID and looking up filename in JS); stuck jobs reset on restart; Clear History button in Jobs tab; anthology detection changed from OR to AND logic (keyword alone no longer sufficient) |
| **0.8.3** | 2026-05-26 | Features: sortable file table columns; job cancellation (kills ffmpeg, cleans up artifacts, resets file to pending); encode button fix for filenames with apostrophes; error message visibility (inline under badge + job history log in Jobs tab) |
| **0.8.2** | 2026-05-26 | Feature: NVIDIA GPU (NVENC) encoding support — opt-in via `ENCODER_BACKEND=nvenc`; auto-detects GPU availability with CPU fallback; dynamic preset/CRF→CQ UI in Settings tab; docker-compose GPU passthrough block |
| **0.8.1** | 2026-05-26 | Bug fixes: skip endpoint, DB race condition, WAL mode, indexes, CRF=0 edge case, scanner skip-count. Deployment: install.sh, update.sh, healthcheck, version label. |
| **0.8.0** | 2026-05-26 | Initial release: scanner, encoder, splitter, Flask UI, SSE progress, Docker deployment |
