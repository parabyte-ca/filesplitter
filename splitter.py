import os
import subprocess
import logging
from collections.abc import Callable

import codec_detector
import scene_detector

logger = logging.getLogger(__name__)


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def split_by_scenes(
    input_path: str,
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[str] | None:
    """
    Detect scenes and split input_path into scene files via stream copy.
    On success, verifies all output files, deletes original, returns list of output paths.
    Returns None on failure.
    """
    probe = codec_detector.probe(input_path)
    if probe is None:
        logger.error("Cannot probe %s", input_path)
        return None

    if progress_cb:
        progress_cb(2.0, "Detecting scenes...")

    scene_cuts = scene_detector.detect_scenes(input_path)

    if not scene_cuts:
        logger.warning("No scene changes detected in %s — skipping split", input_path)
        return None

    stem, ext = os.path.splitext(input_path)
    directory = os.path.dirname(input_path)

    boundaries = [0.0] + scene_cuts + [probe.duration_sec]
    segments = list(zip(boundaries[:-1], boundaries[1:]))

    output_paths: list[str] = []
    total = len(segments)

    for i, (start, end) in enumerate(segments):
        out_path = os.path.join(directory, f"{os.path.basename(stem)}_scene_{i + 1:03d}{ext}")
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
            logger.error("Split failed segment %d: %s", i + 1, result.stderr[-500:])
            _cleanup_files(output_paths)
            return None

        if progress_cb:
            pct = 5.0 + (i + 1) / total * 90.0
            progress_cb(pct, f"Split {i + 1}/{total}: {os.path.basename(out_path)}")

    # Verify all outputs
    if progress_cb:
        progress_cb(96.0, "Verifying scene files...")

    for path in output_paths:
        if not codec_detector.verify_file(path):
            logger.error("Verification failed: %s", path)
            _cleanup_files(output_paths)
            return None

    # Delete original
    try:
        os.unlink(input_path)
        logger.info("Deleted original: %s", input_path)
    except OSError as exc:
        logger.error("Could not delete original %s: %s", input_path, exc)
        # Don't fail — output files are valid

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
