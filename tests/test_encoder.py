import subprocess

import encoder


def _fake_run(returncode: int):
    def run(*a, **k):
        return subprocess.CompletedProcess(args=a, returncode=returncode)
    return run


def test_nvenc_available_first_probe_succeeds(monkeypatch):
    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(0))
    sleeps = []
    monkeypatch.setattr(encoder.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(encoder.glob, "glob", lambda pattern: (_ for _ in ()).throw(
        AssertionError("glob should not be consulted when the first probe succeeds")
    ))

    assert encoder._nvenc_available() is True
    assert sleeps == []


def test_nvenc_available_no_gpu_device_fails_fast(monkeypatch):
    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(1))
    sleeps = []
    monkeypatch.setattr(encoder.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(encoder.glob, "glob", lambda pattern: [])

    assert encoder._nvenc_available() is False
    assert sleeps == []


def test_nvenc_available_retries_and_recovers(monkeypatch):
    attempts = {"n": 0}

    def run(*a, **k):
        attempts["n"] += 1
        return subprocess.CompletedProcess(args=a, returncode=0 if attempts["n"] >= 2 else 1)
    monkeypatch.setattr(encoder.subprocess, "run", run)

    sleeps = []
    monkeypatch.setattr(encoder.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(encoder.glob, "glob", lambda pattern: ["/dev/nvidia0"])
    monkeypatch.setattr(encoder.config, "NVENC_PROBE_MAX_WAIT", 30)

    assert encoder._nvenc_available() is True
    assert sleeps == [5]
    assert attempts["n"] == 2


def test_nvenc_available_gives_up_after_max_wait(monkeypatch):
    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(1))
    monkeypatch.setattr(encoder.glob, "glob", lambda pattern: ["/dev/nvidia0"])
    monkeypatch.setattr(encoder.config, "NVENC_PROBE_MAX_WAIT", 12)

    # Advance a fake monotonic clock on every sleep() call instead of waiting for real time.
    clock = {"t": 0.0}
    monkeypatch.setattr(encoder.time, "monotonic", lambda: clock["t"])
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s
    monkeypatch.setattr(encoder.time, "sleep", fake_sleep)

    assert encoder._nvenc_available() is False
    # 12s max wait / 5s per retry -> 3 retries before the deadline is exceeded
    assert sleeps == [5, 5, 5]
