import os
import re
import subprocess
import tempfile
import logging

import config

logger = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time=([0-9]+(?:\.[0-9]+)?)")


def detect_scenes(video_path: str, threshold: float = None) -> list[float]:
    """
    Return a sorted list of scene-change timestamps (in seconds) for the given file.
    Nearby timestamps closer than MIN_SCENE_DURATION are merged.
    """
    if threshold is None:
        threshold = config.SCENE_THRESHOLD

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        tmp_path = tf.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-filter:v", f"select='gt(scene,{threshold})',metadata=print:file={tmp_path}",
            "-an", "-f", "null", "-",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
        )
        if result.returncode != 0:
            logger.error("Scene detection failed for %s: %s", video_path, result.stderr[-500:])
            return []

        timestamps = _parse_timestamps(tmp_path)
        merged = _merge_nearby(timestamps, config.MIN_SCENE_DURATION)
        logger.info("Detected %d scenes in %s", len(merged) + 1, video_path)
        return merged

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _parse_timestamps(path: str) -> list[float]:
    timestamps: list[float] = []
    try:
        with open(path) as f:
            for line in f:
                m = _PTS_RE.search(line)
                if m:
                    timestamps.append(float(m.group(1)))
    except OSError:
        pass
    return sorted(timestamps)


def _merge_nearby(timestamps: list[float], min_gap: float) -> list[float]:
    """Remove timestamps that are too close together, keeping only ones at least min_gap apart."""
    if not timestamps:
        return []
    result = [timestamps[0]]
    for ts in timestamps[1:]:
        if ts - result[-1] >= min_gap:
            result.append(ts)
    return result
