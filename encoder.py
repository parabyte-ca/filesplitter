import os
import subprocess
import uuid
import logging
from collections.abc import Callable

import config
import codec_detector

logger = logging.getLogger(__name__)


def _scale_filter(resolution: str) -> list[str]:
    """Return ffmpeg scale vf args for the given resolution, or [] for original."""
    height = config.RESOLUTION_MAP.get(resolution)
    if not height:
        return []
    return ["-vf", f"scale=-2:{height}"]


def encode_to_x265(
    input_path: str,
    target_resolution: str = "original",
    crf: int = None,
    preset: str = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> bool:
    """
    Re-encode input_path to x265, replacing the original on success.
    Returns True on success, False on failure.
    progress_cb(pct, log_line) is called periodically with 0–100 progress.
    """
    crf = crf or config.X265_CRF
    preset = preset or config.X265_PRESET

    probe = codec_detector.probe(input_path)
    if probe is None:
        logger.error("Cannot probe %s", input_path)
        return False

    ext = os.path.splitext(input_path)[1]
    tmp_path = os.path.join(
        os.path.dirname(input_path),
        f".tmp_{uuid.uuid4().hex}{ext}",
    )

    scale_args = _scale_filter(target_resolution)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx265",
        "-crf", str(crf),
        "-preset", preset,
        *scale_args,
        "-c:a", "copy",
        "-map_metadata", "0",
        "-progress", "pipe:1",
        "-loglevel", "error",
        tmp_path,
    ]

    logger.info("Encoding %s → x265 [%s crf=%d]", input_path, target_resolution, crf)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        duration_us = int(probe.duration_sec * 1_000_000)
        log_lines: list[str] = []

        for line in proc.stdout:
            line = line.strip()
            log_lines.append(line)
            if len(log_lines) > 50:
                log_lines.pop(0)

            if line.startswith("out_time_us=") and duration_us > 0:
                try:
                    elapsed_us = int(line.split("=")[1])
                    pct = min(100.0, elapsed_us / duration_us * 100)
                    if progress_cb:
                        progress_cb(pct, "\n".join(log_lines[-10:]))
                except ValueError:
                    pass

        proc.wait()

        if proc.returncode != 0:
            stderr = proc.stderr.read()
            logger.error("ffmpeg encode failed: %s", stderr[-500:])
            _cleanup(tmp_path)
            return False

        # Verify the encoded file
        if not codec_detector.verify_file(tmp_path, expected_codec="hevc",
                                          ref_duration=probe.duration_sec):
            logger.error("Verification failed for encoded file: %s", tmp_path)
            _cleanup(tmp_path)
            return False

        os.replace(tmp_path, input_path)
        logger.info("Replaced %s with x265 encode", input_path)
        return True

    except Exception as exc:
        logger.error("Encode error for %s: %s", input_path, exc)
        _cleanup(tmp_path)
        return False


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
