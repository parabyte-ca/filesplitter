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

_PTS_RE    = re.compile(r"pts_time=([0-9]+(?:\.[0-9]+)?)")
_TIME_RE   = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
_BLACK_RE  = re.compile(r"black_start:([0-9.]+).*?black_end:([0-9.]+)")
_FREEZE_RE = re.compile(r"freeze_(start|end):\s*([0-9.]+)")


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
    min_gap: int = None,
) -> list[float] | None:
    """Frame-diff scene detection using ffmpeg select filter.
    Returns list of cut timestamps, [] if none found, None if cancelled/error."""
    if min_gap is None:
        min_gap = config.MIN_SCENE_DURATION

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
        merged = _merge_nearby(timestamps, min_gap)
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
    min_gap: int = None,
    black_min_duration: float = None,
) -> list[float] | None:
    """Black-frame transition detection using ffmpeg blackdetect filter.
    Returns list of cut timestamps (midpoints of black intervals), [] if none, None if cancelled."""
    if min_gap is None:
        min_gap = config.MIN_SCENE_DURATION
    if black_min_duration is None:
        black_min_duration = config.BLACK_MIN_DURATION

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"blackdetect=d={black_min_duration}:pix_th={config.BLACK_PIX_TH}",
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
    merged = _merge_nearby(timestamps, min_gap)
    logger.info("blackdetect: %d scene cuts in %s", len(merged), video_path)
    return merged


def _detect_freeze(
    video_path: str,
    cancel_event: threading.Event | None,
    duration_sec: float,
    progress_cb: Callable | None,
    start_pct: float = 2.0,
    end_pct: float = 33.0,
    min_gap: int = None,
    freeze_min_duration: float = None,
) -> list[float] | None:
    """Static-frame (title card) detection using ffmpeg freezedetect filter.
    Returns midpoints of freeze intervals, [] if none, None if cancelled."""
    if min_gap is None:
        min_gap = config.MIN_SCENE_DURATION
    if freeze_min_duration is None:
        freeze_min_duration = config.SPLIT_FREEZE_MIN_DURATION

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"freezedetect=n=0.001:d={freeze_min_duration}",
        "-an", "-f", "null", "-",
    ]
    rc, stderr_buf = _run_ffmpeg(
        cmd, cancel_event, duration_sec, progress_cb,
        start_pct, end_pct, "Detecting title cards…"
    )
    if rc is None:
        return None  # cancelled
    if rc != 0:
        stderr_tail = "".join(stderr_buf)[-500:]
        logger.error("freezedetect failed for %s: %s", video_path, stderr_tail)
        return []

    freeze_starts: list[float] = []
    freeze_ends: list[float] = []
    for line in stderr_buf:
        m = _FREEZE_RE.search(line)
        if m:
            if m.group(1) == "start":
                freeze_starts.append(float(m.group(2)))
            else:
                freeze_ends.append(float(m.group(2)))

    timestamps = [
        (s + e) / 2
        for s, e in zip(freeze_starts, freeze_ends)
    ]
    timestamps.sort()
    merged = _merge_nearby(timestamps, min_gap)
    logger.info("freezedetect: %d title cards in %s", len(merged), video_path)
    return merged


def _filter_boundary_cuts(cuts: list[float], duration_sec: float, min_gap: float) -> list[float]:
    """Remove cuts that are too close to the start or end of the file.
    Prevents tiny degenerate first/last segments that fail stream-copy verification."""
    if not duration_sec:
        return cuts
    return [t for t in cuts if t >= min_gap and t <= duration_sec - min_gap]


def _limit_cuts(cuts: list[float], target_n: int, duration_sec: float) -> list[float]:
    """Trim cuts to exactly target_n by repeatedly removing the cut that produces
    the shortest adjacent segment. Used when SPLIT_EPISODE_COUNT is set."""
    if target_n <= 0 or len(cuts) <= target_n:
        if 0 < target_n < len(cuts):
            logger.warning(
                "_limit_cuts: only %d cuts found but %d expected — proceeding with %d",
                len(cuts), target_n, len(cuts),
            )
        return cuts

    cuts = list(cuts)
    boundaries = [0.0] + cuts + [duration_sec]

    while len(cuts) > target_n:
        # For each cut, find the length of the shorter of its two adjacent segments.
        # Remove the cut whose shorter neighbour is smallest (least plausible boundary).
        min_val = None
        min_idx = None
        for i, _ in enumerate(cuts):
            left = cuts[i] - boundaries[i]       # boundaries[i] is the previous boundary
            right = boundaries[i + 2] - cuts[i]  # boundaries[i+2] is the next boundary
            shorter = min(left, right)
            if min_val is None or shorter < min_val:
                min_val = shorter
                min_idx = i

        removed = cuts.pop(min_idx)
        boundaries = [0.0] + cuts + [duration_sec]
        logger.debug("_limit_cuts: removed cut at %.1f s (%.0f-s segment)", removed, min_val)

    return cuts


def detect_scenes(
    video_path: str,
    threshold: float = None,
    cancel_event: threading.Event | None = None,
    duration_sec: float = 0,
    progress_cb: Callable[[float, str], None] | None = None,
    *,
    method: str = None,
    min_gap: int = None,
    black_min_duration: float = None,
    freeze_min_duration: float = None,
    target_count: int = 0,
) -> list[float]:
    """
    Return a sorted list of scene-change timestamps (seconds) for the given file.
    Nearby timestamps closer than min_gap (default MIN_SCENE_DURATION) are merged.
    Cuts within min_gap of the file boundaries are discarded.
    Returns [] if cancelled or on error.

    Dispatches based on method (default config.SCENE_METHOD):
      "combined" — black transitions + title cards (static frames); union of both (recommended)
      "black"    — blackdetect only
      "title"    — freezedetect (static frames / title cards) only
      "select"   — frame-diff only
      "auto"     — select first (2%→18%); fall back to blackdetect (18%→33%) if no cuts found

    Optional overrides (keyword-only):
      method              — overrides config.SCENE_METHOD
      min_gap             — overrides config.MIN_SCENE_DURATION
      black_min_duration  — overrides config.BLACK_MIN_DURATION
      freeze_min_duration — overrides config.SPLIT_FREEZE_MIN_DURATION
      target_count        — if >0, trim result to exactly this many cuts via _limit_cuts()
    """
    if threshold is None:
        threshold = config.SCENE_THRESHOLD
    if method is None:
        method = config.SCENE_METHOD
    if min_gap is None:
        min_gap = config.MIN_SCENE_DURATION

    def _finish(cuts):
        cuts = _filter_boundary_cuts(cuts, duration_sec, min_gap)
        if target_count > 0 and duration_sec > 0:
            cuts = _limit_cuts(cuts, target_count, duration_sec)
        return cuts

    if method == "combined":
        # Run black detection (2%→18%) then freeze/title detection (18%→33%), take union.
        black_result = _detect_black(video_path, cancel_event, duration_sec, progress_cb,
                                     start_pct=2.0, end_pct=18.0,
                                     min_gap=min_gap, black_min_duration=black_min_duration)
        if cancel_event and cancel_event.is_set():
            return []
        freeze_result = _detect_freeze(video_path, cancel_event, duration_sec, progress_cb,
                                       start_pct=18.0, end_pct=33.0,
                                       min_gap=min_gap, freeze_min_duration=freeze_min_duration)
        black_cuts = black_result if black_result is not None else []
        freeze_cuts = freeze_result if freeze_result is not None else []
        merged = _merge_nearby(sorted(black_cuts + freeze_cuts), min_gap)
        logger.info("combined: %d total cuts (%d black + %d title) in %s",
                    len(merged), len(black_cuts), len(freeze_cuts), video_path)
        return _finish(merged)

    if method == "black":
        result = _detect_black(video_path, cancel_event, duration_sec, progress_cb,
                               start_pct=2.0, end_pct=33.0,
                               min_gap=min_gap, black_min_duration=black_min_duration)
        return _finish(result if result is not None else [])

    if method == "title":
        result = _detect_freeze(video_path, cancel_event, duration_sec, progress_cb,
                                start_pct=2.0, end_pct=33.0,
                                min_gap=min_gap, freeze_min_duration=freeze_min_duration)
        return _finish(result if result is not None else [])

    # "select" or "auto"
    select_end = 33.0 if method == "select" else 18.0
    cuts = _detect_select(video_path, threshold, cancel_event, duration_sec, progress_cb,
                          start_pct=2.0, end_pct=select_end, min_gap=min_gap)

    if cuts is None:
        return []  # cancelled

    if method == "auto" and not cuts:
        if cancel_event and cancel_event.is_set():
            return []
        if progress_cb:
            progress_cb(18.0, "No cuts found — trying black transition detection…")
        result = _detect_black(video_path, cancel_event, duration_sec, progress_cb,
                               start_pct=18.0, end_pct=33.0,
                               min_gap=min_gap, black_min_duration=black_min_duration)
        cuts = result if result is not None else []

    return _finish(cuts)


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
