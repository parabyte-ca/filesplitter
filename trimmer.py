import os
import subprocess
import uuid
import logging
import threading
from collections.abc import Callable

import config
import codec_detector
import encoder

logger = logging.getLogger(__name__)

TRIM_SECONDS: float = 3.1
DURATION_TOLERANCE_SEC: float = 2.0


def trim_start(
    input_path: str,
    seconds: float = TRIM_SECONDS,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> "codec_detector.ProbeResult | None":
    """
    Re-encode input_path with the first `seconds` cut off the start, replacing the
    original on success. Returns the output file's ProbeResult on success, or None
    on failure or cancellation.
    """
    probe = codec_detector.probe(input_path)
    if probe is None:
        logger.error("Cannot probe %s", input_path)
        return None
    if probe.duration_sec <= seconds:
        logger.error(
            "%s is only %.1fs, cannot trim %.1fs off the start",
            input_path, probe.duration_sec, seconds,
        )
        return None

    ext = os.path.splitext(input_path)[1]
    tmp_path = os.path.join(
        os.path.dirname(input_path),
        f".tmp_{uuid.uuid4().hex}{ext}",
    )

    codec_args = encoder._build_codec_args(config.ENCODER_BACKEND, config.X265_CRF, config.X265_PRESET)
    expected_duration = probe.duration_sec - seconds

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seconds),
        "-i", input_path,
        *codec_args,
        "-c:a", "aac",
        "-map_metadata", "0",
        "-progress", "pipe:1",
        "-loglevel", "error",
        tmp_path,
    ]

    logger.info("Trimming %.1fs off the start of %s", seconds, input_path)

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

        duration_us = int(expected_duration * 1_000_000)
        log_lines: list[str] = []
        current_pct = 0.0

        for line in proc.stdout:
            line = line.strip()
            log_lines.append(line)
            if len(log_lines) > 50:
                log_lines.pop(0)

            if cancel_event and cancel_event.is_set():
                logger.info("Trim cancelled for %s", input_path)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                stderr_thread.join(timeout=2)
                encoder._cleanup(tmp_path)
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
            err_msg = stderr.strip()[-300:] if stderr.strip() else "ffmpeg trim failed (no stderr)"
            logger.error("ffmpeg trim failed: %s", err_msg)
            if progress_cb:
                progress_cb(current_pct, f"ERROR: {err_msg}")
            encoder._cleanup(tmp_path)
            return None

        out_probe = codec_detector.probe(tmp_path)
        err_msg = None
        if out_probe is None:
            err_msg = "Output file verification failed: could not probe output file"
        elif expected_duration > 0:
            diff = abs(out_probe.duration_sec - expected_duration)
            if diff > DURATION_TOLERANCE_SEC:
                err_msg = (
                    f"Output file verification failed: duration {out_probe.duration_sec:.1f}s "
                    f"vs expected {expected_duration:.1f}s (diff {diff:.1f}s, tolerance {DURATION_TOLERANCE_SEC}s)"
                )
        if err_msg:
            logger.error("%s: %s", err_msg, tmp_path)
            if progress_cb:
                progress_cb(current_pct, f"ERROR: {err_msg}")
            encoder._cleanup(tmp_path)
            return None

        os.replace(tmp_path, input_path)
        logger.info("Replaced %s with trimmed version (-%.1fs)", input_path, seconds)
        return out_probe

    except Exception as exc:
        logger.error("Trim error for %s: %s", input_path, exc)
        if progress_cb:
            progress_cb(0, f"ERROR: {exc}")
        encoder._cleanup(tmp_path)
        return None
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
