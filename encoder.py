import os
import subprocess
import uuid
import logging
from collections.abc import Callable

import config
import codec_detector

logger = logging.getLogger(__name__)

# Checked once at startup; no cost at runtime.
def _nvenc_available() -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "hevc_nvenc" in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

NVENC_AVAILABLE: bool = _nvenc_available()

_CPU_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]
_NVENC_PRESETS = ["fast", "medium", "slow", "hq", "hp"]


def _scale_filter(resolution: str) -> list[str]:
    height = config.RESOLUTION_MAP.get(resolution)
    if not height:
        return []
    return ["-vf", f"scale=-2:{height}"]


def _build_codec_args(backend: str, crf: int, preset: str) -> list[str]:
    """Return the ffmpeg codec/quality args for the chosen backend."""
    if backend == "nvenc":
        if not NVENC_AVAILABLE:
            logger.warning("hevc_nvenc not available — falling back to libx265")
            return ["-c:v", "libx265", "-crf", str(crf), "-preset", "medium"]
        nvenc_preset = preset if preset in _NVENC_PRESETS else "medium"
        return ["-c:v", "hevc_nvenc", "-rc", "vbr", "-cq", str(crf), "-preset", nvenc_preset]
    # Default: CPU libx265
    cpu_preset = preset if preset in _CPU_PRESETS else "medium"
    return ["-c:v", "libx265", "-crf", str(crf), "-preset", cpu_preset]


def encode_to_x265(
    input_path: str,
    target_resolution: str = "original",
    crf: int = None,
    preset: str = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> bool:
    """
    Re-encode input_path to HEVC, replacing the original on success.
    Uses libx265 (CPU) or hevc_nvenc (GPU) depending on ENCODER_BACKEND config.
    Returns True on success, False on failure.
    """
    if crf is None:
        crf = config.X265_CRF
    if preset is None:
        preset = config.X265_PRESET

    backend = config.ENCODER_BACKEND

    probe = codec_detector.probe(input_path)
    if probe is None:
        logger.error("Cannot probe %s", input_path)
        return False

    ext = os.path.splitext(input_path)[1]
    tmp_path = os.path.join(
        os.path.dirname(input_path),
        f".tmp_{uuid.uuid4().hex}{ext}",
    )

    codec_args = _build_codec_args(backend, crf, preset)
    scale_args = _scale_filter(target_resolution)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        *codec_args,
        *scale_args,
        "-c:a", "copy",
        "-map_metadata", "0",
        "-progress", "pipe:1",
        "-loglevel", "error",
        tmp_path,
    ]

    effective_backend = "nvenc" if "hevc_nvenc" in codec_args else "cpu"
    logger.info(
        "Encoding %s → hevc [backend=%s res=%s crf/cq=%d]",
        input_path, effective_backend, target_resolution, crf,
    )

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

        if not codec_detector.verify_file(tmp_path, expected_codec="hevc",
                                          ref_duration=probe.duration_sec):
            logger.error("Verification failed for encoded file: %s", tmp_path)
            _cleanup(tmp_path)
            return False

        os.replace(tmp_path, input_path)
        logger.info("Replaced %s with hevc encode (%s)", input_path, effective_backend)
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
