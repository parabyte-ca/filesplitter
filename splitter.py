import os
import subprocess
import threading
import logging
from collections.abc import Callable

import codec_detector
import config
import scene_detector
import scene_namer

logger = logging.getLogger(__name__)


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _scene_path(directory: str, stem: str, ext: str, index: int, online_names: list | None) -> str:
    """Generate output path for scene `index` (0-based), using online name if available."""
    if online_names:
        name = scene_namer.sanitize(online_names[index])
        return os.path.join(directory, f"{index + 1:02d} - {name}{ext}")
    base = os.path.basename(stem)
    return os.path.join(directory, f"{base} {index + 1:02d}{ext}")


def split_by_scenes(
    input_path: str,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[str] | None:
    """
    Detect scenes and split input_path into scene files via stream copy.
    On success, verifies all output files, deletes original, returns list of output paths.
    Returns None on failure or cancellation (check cancel_event.is_set() to distinguish).
    """
    probe = codec_detector.probe(input_path)
    if probe is None:
        logger.error("Cannot probe %s", input_path)
        return None

    if progress_cb:
        progress_cb(2.0, "Detecting scenes…")

    scene_cuts = scene_detector.detect_scenes(
        input_path,
        cancel_event=cancel_event,
        duration_sec=probe.duration_sec or 0,
        progress_cb=progress_cb,
    )

    if cancel_event and cancel_event.is_set():
        return None

    if not scene_cuts:
        logger.warning("No scene changes detected in %s — skipping split", input_path)
        if progress_cb:
            progress_cb(0, "ERROR: No scene changes detected in this file")
        return None

    stem, ext = os.path.splitext(input_path)
    directory = os.path.dirname(input_path)

    boundaries = [0.0] + scene_cuts + [probe.duration_sec]
    segments = list(zip(boundaries[:-1], boundaries[1:]))
    total = len(segments)

    # --- Online scene naming ---
    online_names = None
    if config.TPDB_API_KEY:
        if progress_cb:
            progress_cb(34.0, "Identifying scenes online…")
        online_names = scene_namer.get_scene_names(
            input_path,
            segments,
            config.TPDB_API_KEY,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
        )
        if cancel_event and cancel_event.is_set():
            return None

    if online_names:
        logger.info("Online scene names found for %s", os.path.basename(input_path))
    else:
        logger.info("No online scene names — using numbered fallback for %s", os.path.basename(input_path))

    # --- Split segments ---
    output_paths: list[str] = []

    for i, (start, end) in enumerate(segments):
        if cancel_event and cancel_event.is_set():
            logger.info("Split cancelled for %s after %d/%d segments", input_path, i, total)
            _cleanup_files(output_paths)
            return None

        out_path = _scene_path(directory, stem, ext, i, online_names)
        output_paths.append(out_path)

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-ss", _format_ts(start),
            "-to", _format_ts(end),
            "-c", "copy",
            "-map_metadata", "0",
            "-loglevel", "error",
            out_path,
        ]

        logger.info(
            "Splitting segment %d/%d: %s → %s",
            i + 1, total, _format_ts(start), _format_ts(end),
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            err_msg = result.stderr.strip()[-300:] if result.stderr.strip() else "ffmpeg split failed"
            logger.error("Split failed segment %d: %s", i + 1, err_msg)
            pct = 37.0 + i / total * 55.0
            if progress_cb:
                progress_cb(pct, f"ERROR: Segment {i + 1}/{total} failed — {err_msg}")
            _cleanup_files(output_paths)
            return None

        pct = 37.0 + (i + 1) / total * 55.0
        if progress_cb:
            progress_cb(pct, f"Split {i + 1}/{total}: {os.path.basename(out_path)}")

    if progress_cb:
        progress_cb(96.0, "Verifying scene files…")

    for path in output_paths:
        if not codec_detector.verify_file(path):
            err_msg = f"Verification failed: {os.path.basename(path)}"
            logger.error(err_msg)
            if progress_cb:
                progress_cb(96.0, f"ERROR: {err_msg}")
            _cleanup_files(output_paths)
            return None

    try:
        os.unlink(input_path)
        logger.info("Deleted original: %s", input_path)
    except OSError as exc:
        logger.error("Could not delete original %s: %s", input_path, exc)

    if progress_cb:
        progress_cb(100.0, f"Done — {total} scenes created")

    return output_paths


def _cleanup_files(paths: list[str]) -> None:
    for p in paths:
        try:
            if os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass
