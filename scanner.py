import os
import logging

import config
import db
import codec_detector

logger = logging.getLogger(__name__)


def _is_anthology(filename: str, size_bytes: int, duration_sec: float) -> bool:
    long_enough = bool(duration_sec and duration_sec >= config.SPLIT_MIN_DURATION)
    big_enough  = bool(size_bytes  and size_bytes  >= config.SPLIT_MIN_SIZE)
    return long_enough or big_enough


def scan_all() -> dict:
    """Scan all configured MEDIA_PATHS and upsert file records. Returns summary."""
    found = skipped = errors = 0

    for media_path in config.MEDIA_PATHS:
        if not os.path.isdir(media_path):
            logger.warning("Media path not found: %s", media_path)
            continue

        for root, dirs, files in os.walk(media_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in config.VIDEO_EXTENSIONS:
                    continue

                full_path = os.path.join(root, fname)

                try:
                    # Skip probing files already finished — avoids re-scanning
                    # large completed libraries on every scan.
                    existing = db.get_file_by_path(full_path)
                    if existing and existing["status"] in ("done", "skipped"):
                        skipped += 1
                        continue

                    info = codec_detector.probe(full_path)
                    if info is None:
                        logger.warning("ffprobe failed: %s", full_path)
                        errors += 1
                        continue

                    anthology = _is_anthology(fname, info.size_bytes, info.duration_sec)
                    db.upsert_file(
                        path=full_path,
                        filename=fname,
                        size_bytes=info.size_bytes,
                        duration_sec=info.duration_sec,
                        codec=info.codec,
                        is_anthology=anthology,
                    )
                    found += 1
                    logger.info(
                        "Found: %s | codec=%s | %.0fs | anthology=%s",
                        fname, info.codec, info.duration_sec, anthology,
                    )

                except Exception as exc:
                    logger.error("Error scanning %s: %s", full_path, exc)
                    errors += 1

    return {"found": found, "skipped": skipped, "errors": errors}
