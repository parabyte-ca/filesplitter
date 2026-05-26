# FileSplitter

**Version 0.9.1**

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

This pulls the latest code, rebuilds the image, and restarts the container with zero manual steps.

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

---

## Configuration

All settings are environment variables in `docker-compose.yml`. The dashboard Settings tab lets you change encoding and scene-detection parameters at runtime without a restart.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_PATHS` | `/media` | Comma-separated container paths to scan |
| `FLASK_PORT` | `4250` | Dashboard port |
| `SCENE_THRESHOLD` | `0.4` | Scene detection sensitivity (0=everything, 1=nothing) |
| `MIN_SCENE_DURATION` | `120` | Minimum seconds between split points |
| `SPLIT_MIN_DURATION` | `3600` | Flag as anthology if duration ≥ this (seconds) |
| `SPLIT_MIN_SIZE` | `4294967296` | Flag as anthology if size ≥ this (bytes, default 4 GB) |
| `SPLIT_KEYWORDS` | `Vol,Compilation,…` | Filename keywords that trigger anthology classification |
| `ENCODER_BACKEND` | `cpu` | `cpu` (libx265) or `nvenc` (GPU HEVC — see GPU section above) |
| `X265_CRF` | `28` | x265 CRF / NVENC CQ quality (18=high, 28=good, 35=small) |
| `X265_PRESET` | `medium` | Encoding speed preset (CPU: ultrafast→veryslow; NVENC: fast/medium/slow/hq/hp) |
| `TARGET_RESOLUTION` | `original` | Output resolution: `original`, `1080p`, `720p`, etc. |
| `MAX_WORKERS` | `2` | Simultaneous ffmpeg jobs |

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

---

## Revision History

| Version | Date | Notes |
|---|---|---|
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
