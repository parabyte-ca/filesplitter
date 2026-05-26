import logging
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable

import requests

logger = logging.getLogger(__name__)

TPDB_BASE = "https://api.theporndb.net"

# Junk tokens to strip when cleaning a filename for title search.
_JUNK = re.compile(
    r'\b('
    r'4[Kk]|2160[pP]|1080[pP]|720[pP]|480[pP]|SD|HD|Full\.?HD'
    r'|HEVC|H\.?265|H\.?264|x265|x264|AVC'
    r'|WEBRip|BluRay|BDRip|HDRip|DVDRip|WEB-DL'
    r'|XXX|Porn\.?Movie|online|repack'
    r')\b',
    re.IGNORECASE,
)
_DATE_PREFIX = re.compile(r'^\d{1,2}[\s._-]\d{1,2}[\s._-]\d{4}\s*')
_DATE_PREFIX2 = re.compile(r'^\d{4}[\s._-]\d{1,2}[\s._-]\d{1,2}\s*')


def clean_title(filename_stem: str) -> str:
    """Produce a human-readable, searchable title from a raw filename stem."""
    s = filename_stem.replace('.', ' ').replace('_', ' ')
    s = _DATE_PREFIX.sub('', s)
    s = _DATE_PREFIX2.sub('', s)
    s = _JUNK.sub('', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def sanitize(name: str) -> str:
    """Strip filesystem-invalid characters, collapse whitespace, cap at 180 chars."""
    name = re.sub(r'[/\\\x00:"*?<>|]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:180] or "Scene"


def _tpdb_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "FileSplitter/1.0",
    }


def _extract_keyframe(video_path: str, timestamp: float) -> str | None:
    """Extract a single JPEG frame at `timestamp` seconds into a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "5",
        "-loglevel", "error",
        path,
    ]
    try:
        ok = subprocess.run(cmd, timeout=30, capture_output=True).returncode == 0
        if ok and os.path.getsize(path) > 0:
            return path
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        os.unlink(path)
    except OSError:
        pass
    return None


def _phash_int(image_path: str) -> int | None:
    """Compute 64-bit perceptual hash of an image. Returns integer or None."""
    try:
        import imagehash
        from PIL import Image
        img = Image.open(image_path)
        return int(str(imagehash.phash(img)), 16)
    except Exception as exc:
        logger.debug("pHash computation failed: %s", exc)
        return None


def _query_phash(phash_int: int, api_key: str) -> dict | None:
    try:
        r = requests.get(
            f"{TPDB_BASE}/scenes",
            params={"phash": phash_int},
            headers=_tpdb_headers(api_key),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return data[0] if data else None
    except Exception as exc:
        logger.debug("TPDB phash query failed: %s", exc)
        return None


def _query_parse(filename_stem: str, api_key: str) -> list[dict]:
    try:
        r = requests.get(
            f"{TPDB_BASE}/scenes",
            params={"parse": filename_stem},
            headers=_tpdb_headers(api_key),
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as exc:
        logger.debug("TPDB parse query failed: %s", exc)
        return []


def _format_name(result: dict, index: int) -> str:
    """Build a scene name from a ThePornDB result dict."""
    title = (result.get("title") or "").strip()
    performers = [
        p.get("name", "") for p in result.get("performers", []) if p.get("name")
    ]
    if title:
        return title
    if performers:
        return " & ".join(performers[:3])
    return f"Scene {index + 1}"


def get_scene_names(
    input_path: str,
    segments: list[tuple[float, float]],
    api_key: str,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[str] | None:
    """
    Try to identify a name for each scene segment using ThePornDB.

    Strategy:
    1. Per-scene: extract keyframe → compute pHash → query /scenes?phash=
    2. If <50% matched, try title-based fallback: /scenes?parse={filename}
    3. Return None if all lookups fail (caller uses numbered fallback).

    Returns a list of raw (unsanitized) names of length == len(segments), or None.
    """
    if not segments:
        return None

    n = len(segments)
    names: list[str | None] = [None] * n
    matched = 0

    for i, (start, end) in enumerate(segments):
        if cancel_event and cancel_event.is_set():
            return None

        midpoint = (start + end) / 2.0
        frame_path = _extract_keyframe(input_path, midpoint)
        if frame_path is None:
            continue

        try:
            phash = _phash_int(frame_path)
            if phash is None:
                continue
            result = _query_phash(phash, api_key)
            if result:
                names[i] = _format_name(result, i)
                matched += 1
                logger.info("pHash match for scene %d: %s", i + 1, names[i])
        finally:
            try:
                os.unlink(frame_path)
            except OSError:
                pass

        if progress_cb:
            pct = 34.0 + (i + 1) / n * 3.0  # 34–37% during lookup
            progress_cb(pct, f"Identifying scenes… ({i + 1}/{n})")

    # If at least half the scenes matched via pHash, fill gaps and return.
    if matched >= n / 2:
        for i in range(n):
            if names[i] is None:
                names[i] = f"Scene {i + 1}"
        return names  # type: ignore[return-value]

    # Fall back to title-based parse search.
    stem = os.path.splitext(os.path.basename(input_path))[0]
    title_results = _query_parse(stem, api_key)
    if len(title_results) == n:
        logger.info("Title-parse fallback: %d scenes matched for %s", n, stem)
        return [_format_name(r, i) for i, r in enumerate(title_results)]

    # Also try with cleaned title.
    clean = clean_title(stem)
    if clean != stem:
        title_results = _query_parse(clean, api_key)
        if len(title_results) == n:
            logger.info("Clean-title fallback: %d scenes matched for %s", n, clean)
            return [_format_name(r, i) for i, r in enumerate(title_results)]

    logger.info("No online scene names found for %s", stem)
    return None
