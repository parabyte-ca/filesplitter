import os
import re
import subprocess
import tempfile
import threading
import time
import logging

import config

logger = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time=([0-9]+(?:\.[0-9]+)?)")


def detect_scenes(
    video_path: str,
    threshold: float = None,
    cancel_event: threading.Event | None = None,
) -> list[float]:
    """
    Return a sorted list of scene-change timestamps (seconds) for the given file.
    Nearby timestamps closer than MIN_SCENE_DURATION are merged.
    Returns [] if cancelled or on error.
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
        proc = subprocess.Popen(cmd, capture_output=True, text=True)

        # Poll until complete, checking for cancel every 0.5s.
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                logger.info("Scene detection cancelled for %s", video_path)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return []
            time.sleep(0.5)

        if proc.returncode != 0:
            stderr = proc.stderr.read() if proc.stderr else ""
            logger.error("Scene detection failed for %s: %s", video_path, stderr[-500:])
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
    if not timestamps:
        return []
    result = [timestamps[0]]
    for ts in timestamps[1:]:
        if ts - result[-1] >= min_gap:
            result.append(ts)
    return result
