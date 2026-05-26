import os
import re
import subprocess
import tempfile
import threading
import time
import logging
from collections.abc import Callable

import config

logger = logging.getLogger(__name__)

_PTS_RE  = re.compile(r"pts_time=([0-9]+(?:\.[0-9]+)?)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def _drain_stderr(
    proc: subprocess.Popen,
    buf: list,
    duration_sec: float,
    progress_cb: Callable | None,
) -> None:
    """Read proc.stderr line-by-line to prevent pipe-buffer deadlock.
    Optionally parses ffmpeg's time= field to report progress (2–33%)."""
    for line in proc.stderr:
        buf.append(line)
        if progress_cb and duration_sec > 0:
            m = _TIME_RE.search(line)
            if m:
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = 2.0 + (t / duration_sec) * 31.0
                progress_cb(min(pct, 33.0), "Detecting scenes…")


def detect_scenes(
    video_path: str,
    threshold: float = None,
    cancel_event: threading.Event | None = None,
    duration_sec: float = 0,
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[float]:
    """
    Return a sorted list of scene-change timestamps (seconds) for the given file.
    Nearby timestamps closer than MIN_SCENE_DURATION are merged.
    Returns [] if cancelled or on error.

    duration_sec + progress_cb: when both are provided, incremental progress is
    reported from 2% to 33% by parsing ffmpeg's time= output.
    """
    if threshold is None:
        threshold = config.SCENE_THRESHOLD

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        tmp_path = tf.name

    stderr_buf: list[str] = []

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-filter:v", f"select='gt(scene,{threshold})',metadata=print:file={tmp_path}",
            "-an", "-f", "null", "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        drain_thread = threading.Thread(
            target=_drain_stderr,
            args=(proc, stderr_buf, duration_sec, progress_cb),
            daemon=True,
        )
        drain_thread.start()

        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                logger.info("Scene detection cancelled for %s", video_path)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                drain_thread.join(timeout=5)
                return []
            time.sleep(0.5)

        drain_thread.join(timeout=10)

        if proc.returncode != 0:
            stderr_tail = "".join(stderr_buf)[-500:]
            logger.error("Scene detection failed for %s: %s", video_path, stderr_tail)
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
