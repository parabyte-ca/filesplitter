import os
import subprocess

import pytest

import codec_detector
import config


def _make_valid_video(path: str, duration: float = 1) -> None:
    """Generate a tiny real video file via ffmpeg (used to test the success path)."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=32x32:d={duration}",
            "-c:v", "libx264", "-loglevel", "error", path,
        ],
        check=True, timeout=30,
    )


def test_probe_with_error_file_not_found(tmp_path):
    result, err = codec_detector.probe_with_error(str(tmp_path / "nope.mp4"))
    assert result is None
    assert "not found" in err.lower()


def test_probe_with_error_empty_file(tmp_path):
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"")
    result, err = codec_detector.probe_with_error(str(path))
    assert result is None
    assert err  # some specific reason, not silently None
    assert "ffprobe failed" in err or "invalid output" in err


def test_probe_with_error_garbage_file(tmp_path):
    path = tmp_path / "garbage.mp4"
    path.write_bytes(b"this is not a video file" * 100)
    result, err = codec_detector.probe_with_error(str(path))
    assert result is None
    assert err


def test_probe_with_error_valid_video(tmp_path):
    path = tmp_path / "valid.mp4"
    _make_valid_video(str(path))
    result, err = codec_detector.probe_with_error(str(path))
    assert err is None
    assert result is not None
    assert result.codec == "h264"
    assert result.width == 32
    assert result.height == 32


def test_probe_with_error_timeout(tmp_path, monkeypatch):
    path = tmp_path / "valid.mp4"
    _make_valid_video(str(path))
    monkeypatch.setattr(config, "FFPROBE_TIMEOUT", 60)

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=60)
    monkeypatch.setattr(codec_detector.subprocess, "run", fake_run)

    result, err = codec_detector.probe_with_error(str(path))
    assert result is None
    assert "timed out after 60s" in err


def test_probe_with_error_no_video_stream(tmp_path):
    # An audio-only file: valid media, but no video stream.
    path = tmp_path / "audio.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
         "-loglevel", "error", str(path)],
        check=True, timeout=30,
    )
    result, err = codec_detector.probe_with_error(str(path))
    assert result is None
    assert "no video stream" in err.lower()


def test_probe_matches_probe_with_error_on_success(tmp_path):
    path = tmp_path / "valid.mp4"
    _make_valid_video(str(path))
    plain = codec_detector.probe(str(path))
    verbose, err = codec_detector.probe_with_error(str(path))
    assert err is None
    assert plain.codec == verbose.codec
    assert plain.width == verbose.width


def test_probe_returns_none_on_failure_without_raising(tmp_path):
    assert codec_detector.probe(str(tmp_path / "nope.mp4")) is None
