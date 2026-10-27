import glob
import os
import subprocess
import time
import uuid
import logging
import threading
from collections.abc import Callable

import config
import codec_detector

logger = logging.getLogger(__name__)

_NVENC_PROBE_RETRY_SECS = 5
_NVENC_BACKGROUND_RETRY_SECS = 300


def _nvenc_probe_once() -> bool:
    """Actually attempt a tiny real hevc_nvenc encode. This is the only way to tell
    whether the GPU/driver is truly usable — `ffmpeg -encoders` lists hevc_nvenc as
    soon as it was compiled in, regardless of whether a GPU is even present, since
    NVENC support is dlopen'd at encode time rather than linked at build time."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-c:v", "hevc_nvenc", "-frames:v", "1", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _nvenc_available() -> bool:
    """Checked once at startup; no cost at runtime.

    A GPU passed through via docker-compose.yml's `deploy.reservations` block shows up
    as /dev/nvidia* device files the instant the container is created, but the NVIDIA
    driver on the host can still be mid-initialization right after a reboot, so the
    very first real encode attempt can transiently fail. Only retry when a GPU was
    actually configured for this container — non-GPU installs see no added delay.
    """
    if _nvenc_probe_once():
        return True

    if not glob.glob("/dev/nvidia*"):
        return False  # no GPU configured for this container at all

    deadline = time.monotonic() + config.NVENC_PROBE_MAX_WAIT
    while time.monotonic() < deadline:
        logger.warning(
            "GPU device present but hevc_nvenc probe failed — driver may still be "
            "initializing, retrying in %ds", _NVENC_PROBE_RETRY_SECS,
        )
        time.sleep(_NVENC_PROBE_RETRY_SECS)
        if _nvenc_probe_once():
            logger.info("hevc_nvenc became available after retry")
            return True

    logger.warning(
        "hevc_nvenc still unavailable after %ds — falling back to CPU for now; will "
        "keep checking every %ds in the background, or use Settings → Recheck GPU",
        config.NVENC_PROBE_MAX_WAIT, _NVENC_BACKGROUND_RETRY_SECS,
    )
    threading.Thread(target=_background_retry_loop, daemon=True).start()
    return False


def _background_retry_loop() -> None:
    """Keeps checking every few minutes after the boot-time probe gave up, so a GPU
    whose driver took longer than NVENC_PROBE_MAX_WAIT to come up still gets picked
    up on its own — no container restart required. Stops once it succeeds."""
    while True:
        time.sleep(_NVENC_BACKGROUND_RETRY_SECS)
        if recheck_nvenc():
            logger.info("hevc_nvenc became available via background retry")
            return


def recheck_nvenc() -> bool:
    """Re-run a single NVENC probe on demand (the Settings → "Recheck GPU" button)
    and update the live availability flag. Unlike the boot-time check, this doesn't
    loop/retry internally — the caller can just invoke it again if the driver still
    isn't ready, rather than this call blocking for up to NVENC_PROBE_MAX_WAIT seconds.
    """
    global NVENC_AVAILABLE
    NVENC_AVAILABLE = _nvenc_probe_once()
    return NVENC_AVAILABLE


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
    if backend == "nvenc":
        if not NVENC_AVAILABLE:
            logger.warning("hevc_nvenc not available — falling back to libx265")
            return ["-c:v", "libx265", "-crf", str(crf), "-preset", "medium"]
        nvenc_preset = preset if preset in _NVENC_PRESETS else "medium"
        return ["-c:v", "hevc_nvenc", "-cq", str(crf), "-b:v", "0", "-preset", nvenc_preset]
    cpu_preset = preset if preset in _CPU_PRESETS else "medium"
    return ["-c:v", "libx265", "-crf", str(crf), "-preset", cpu_preset]


def encode_to_x265(
    input_path: str,
    target_resolution: str = "original",
    crf: int | None = None,
    preset: str | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> "codec_detector.ProbeResult | None":
    """
    Re-encode input_path to HEVC, replacing the original on success.
    Returns the output file's ProbeResult (codec/width/height/duration/size) on success,
    or None on failure or cancellation.
    Cancellation is signalled via cancel_event; check cancel_event.is_set() to distinguish.
    """
    if crf is None:
        crf = config.X265_CRF
    if preset is None:
        preset = config.X265_PRESET

    backend = config.ENCODER_BACKEND

    probe, probe_err = codec_detector.probe_with_error(input_path)
    if probe is None:
        err_msg = f"Cannot read video info: {probe_err}"
        logger.error("%s: %s", err_msg, input_path)
        if progress_cb:
            progress_cb(0, f"ERROR: {err_msg}")
        return None

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

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Drain stderr in a background thread to prevent the 64 KB pipe buffer
        # from filling up and deadlocking ffmpeg when stdout stops producing output.
        stderr_buf: list[str] = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_buf.extend(proc.stderr),
            daemon=True,
        )
        stderr_thread.start()

        # Cancel-watcher: terminates proc as soon as cancel_event fires, even if
        # ffmpeg hasn't produced any stdout yet (e.g. during initial header scan).
        if cancel_event:
            def _watch_cancel() -> None:
                cancel_event.wait()
                if proc.poll() is None:
                    proc.terminate()
            threading.Thread(target=_watch_cancel, daemon=True).start()

        duration_us = int(probe.duration_sec * 1_000_000)
        log_lines: list[str] = []
        current_pct = 0.0

        for line in proc.stdout:
            line = line.strip()
            log_lines.append(line)
            if len(log_lines) > 50:
                log_lines.pop(0)

            if cancel_event and cancel_event.is_set():
                logger.info("Encode cancelled for %s", input_path)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                stderr_thread.join(timeout=2)
                _cleanup(tmp_path)
                return None

            if line.startswith("out_time_us=") and duration_us > 0:
                try:
                    elapsed_us = int(line.split("=")[1])
                    current_pct = min(100.0, elapsed_us / duration_us * 100)
                    if progress_cb:
                        progress_cb(current_pct, "\n".join(log_lines[-10:]))
                except ValueError:
                    pass

        proc.wait()
        stderr_thread.join(timeout=2)

        if proc.returncode != 0:
            stderr = "".join(stderr_buf)
            err_msg = stderr.strip()[-300:] if stderr.strip() else "ffmpeg encode failed (no stderr)"
            logger.error("ffmpeg encode failed: %s", err_msg)
            if progress_cb:
                progress_cb(current_pct, f"ERROR: {err_msg}")
            _cleanup(tmp_path)
            return None

        out_probe = codec_detector.probe(tmp_path)
        err_msg = None
        if out_probe is None:
            err_msg = "Output file verification failed: could not probe output file"
        elif not out_probe.is_x265:
            err_msg = f"Output file verification failed: codec is '{out_probe.codec}', expected hevc"
        elif probe.duration_sec > 0:
            diff_pct = abs(out_probe.duration_sec - probe.duration_sec) / probe.duration_sec * 100
            if diff_pct > 10.0:
                err_msg = (
                    f"Output file verification failed: duration {out_probe.duration_sec:.0f}s "
                    f"vs expected {probe.duration_sec:.0f}s ({diff_pct:.1f}% diff, tolerance 10%)"
                )
        if err_msg:
            logger.error("%s: %s", err_msg, tmp_path)
            if progress_cb:
                progress_cb(current_pct, f"ERROR: {err_msg}")
            _cleanup(tmp_path)
            return None

        os.replace(tmp_path, input_path)
        logger.info("Replaced %s with hevc encode (%s)", input_path, effective_backend)
        return out_probe

    except Exception as exc:
        logger.error("Encode error for %s: %s", input_path, exc)
        if progress_cb:
            progress_cb(0, f"ERROR: {exc}")
        _cleanup(tmp_path)
        return None
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
