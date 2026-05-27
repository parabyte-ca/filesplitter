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

_PTS_RE   = re.compile(r"pts_time=([0-9]+(?:\.[0-9]+)?)")
_TIME_RE  = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
_BLACK_RE = re.compile(r"black_start:([0-9.]+).*?black_end:([0-9.]+)")


def _drain_stderr(
    proc: subprocess.Popen,
    buf: list,
    duration_sec: float,
    progress_cb: Callable | None,
    start_pct: float = 2.0,
    end_pct: float = 33.0,
    label: str = "Detecting scenes…",
) -> None:
    """Read proc.stderr line-by-line to prevent pipe-buffer deadlock.
    Parses ffmpeg's time= field to report progress scaled to [start_pct, end_pct]."""
    for line in proc.stderr:
        buf.append(line)
        if progress_cb and duration_sec > 0:
            m = _TIME_RE.search(line)
            if m:
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = start_pct + (t / duration_sec) * (end_pct - start_pct)
                progress_cb(min(pct, end_pct), label)


def _run_ffmpeg(cmd, cancel_event, duration_sec, progress_cb, start_pct, end_pct, label):
    """Launch an ffmpeg subprocess, drain stderr in a thread, poll for cancel.
    Returns (returncode, stderr_buf)."""
    stderr_buf: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    drain_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc, stderr_buf, duration_sec, progress_cb, start_pct, end_pct, label),
        daemon=True,
    )
    drain_thread.start()

    while proc.poll() is None:
        if cancel_event and cancel_event.is_set():
            logger.info("Scene detection cancelled")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            drain_thread.join(timeout=5)
            return None, stderr_buf
        time.sleep(0.5)

    drain_thread.join(timeout=10)
    return proc.returncode, stderr_buf


def _detect_select(
    video_path: str,
    threshold: float,
    cancel_event: threading.Event | None,
    duration_sec: float,
    progress_cb: Callable | None,
    start_pct: float = 2.0,
    end_pct: float = 33.0,
) -> list[float] | None:
    """Frame-diff scene detection using ffmpeg select filter.
    Returns list of cut timestamps, [] if none found, None if cancelled/error."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        tmp_path = tf.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-filter:v", f"select='gt(scene,{threshold})',metadata=print:file={tmp_path}",
            "-an", "-f", "null", "-",
        ]
        rc, stderr_buf = _run_ffmpeg(
            cmd, cancel_event, duration_sec, progress_cb,
            start_pct, end_pct, "Detecting scenes…"
        )
        if rc is None:
            return None  # cancelled
        if rc != 0:
            stderr_tail = "".join(stderr_buf)[-500:]
            logger.error("select detection failed for %s: %s", video_path, stderr_tail)
            return []

        timestamps = _parse_timestamps(tmp_path)
        merged = _merge_nearby(timestamps, config.MIN_SCENE_DURATION)
        logger.info("select: %d scene cuts in %s", len(merged), video_path)
        return merged

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _detect_black(
    video_path: str,
    cancel_event: threading.Event | None,
    duration_sec: float,
    progress_cb: Callable | None,
    start_pct: float = 2.0,
    end_pct: float = 33.0,
) -> list[float] | None:
    """Black-frame transition detection using ffmpeg blackdetect filter.
    Returns list of cut timestamps (midpoints of black intervals), [] if none, None if cancelled."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"blackdetect=d={config.BLACK_MIN_DURATION}:pix_th={config.BLACK_PIX_TH}",
        "-an", "-f", "null", "-",
    ]
    rc, stderr_buf = _run_ffmpeg(
        cmd, cancel_event, duration_sec, progress_cb,
        start_pct, end_pct, "Detecting black transitions…"
    )
    if rc is None:
        return None  # cancelled
    if rc != 0:
        stderr_tail = "".join(stderr_buf)[-500:]
        logger.error("blackdetect failed for %s: %s", video_path, stderr_tail)
        return []

    timestamps = []
    for line in stderr_buf:
        m = _BLACK_RE.search(line)
        if m:
            midpoint = (float(m.group(1)) + float(m.group(2))) / 2
            timestamps.append(midpoint)

    timestamps.sort()
    merged = _merge_nearby(timestamps, config.MIN_SCENE_DURATION)
    logger.info("blackdetect: %d scene cuts in %s", len(merged), video_path)
    return merged


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

    Dispatches based on config.SCENE_METHOD:
      "select" — frame-diff only (original behaviour)
      "black"  — blackdetect only (best for fade-to-black anthology content)
      "auto"   — select first (2%→18%); fall back to blackdetect (18%→33%) if no cuts found
    """
    if threshold is None:
        threshold = config.SCENE_THRESHOLD

    method = config.SCENE_METHOD

    if method == "black":
        result = _detect_black(video_path, cancel_event, duration_sec, progress_cb,
                               start_pct=2.0, end_pct=33.0)
        return result if result is not None else []

    # "select" or "auto"
    select_end = 33.0 if method == "select" else 18.0
    cuts = _detect_select(video_path, threshold, cancel_event, duration_sec, progress_cb,
                          start_pct=2.0, end_pct=select_end)

    if cuts is None:
        return []  # cancelled

    if method == "auto" and not cuts:
        if cancel_event and cancel_event.is_set():
            return []
        if progress_cb:
            progress_cb(18.0, "No cuts found — trying black transition detection…")
        result = _detect_black(video_path, cancel_event, duration_sec, progress_cb,
                               start_pct=18.0, end_pct=33.0)
        return result if result is not None else []

    return cuts


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
