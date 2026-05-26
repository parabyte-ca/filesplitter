# FileSplitter

**Version 0.8.1**

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
| `X265_CRF` | `28` | x265 quality (18=high, 28=good, 35=small) |
| `X265_PRESET` | `medium` | x265 speed/compression preset |
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
| **0.8.1** | 2026-05-26 | Bug fixes: skip endpoint, DB race condition, WAL mode, indexes, CRF=0 edge case, scanner skip-count. Deployment: install.sh, update.sh, healthcheck, version label. |
| **0.8.0** | 2026-05-26 | Initial release: scanner, encoder, splitter, Flask UI, SSE progress, Docker deployment |
