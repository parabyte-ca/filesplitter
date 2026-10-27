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


class _FakeThread:
    """Stands in for threading.Thread so the background-retry thread is never
    actually started (and its real-time sleep never runs) during this test — it just
    records that one was scheduled with the right target/daemon flag."""
    started = []

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target
        self.daemon = daemon

    def start(self):
        _FakeThread.started.append(self)


def test_nvenc_available_gives_up_after_max_wait(monkeypatch):
    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(1))
    monkeypatch.setattr(encoder.glob, "glob", lambda pattern: ["/dev/nvidia0"])
    monkeypatch.setattr(encoder.config, "NVENC_PROBE_MAX_WAIT", 12)
    _FakeThread.started = []
    monkeypatch.setattr(encoder.threading, "Thread", _FakeThread)

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
    # A background retry thread is scheduled (but never actually run here) so the
    # driver can still be picked up later without a container restart.
    assert len(_FakeThread.started) == 1
    assert _FakeThread.started[0].target is encoder._background_retry_loop
    assert _FakeThread.started[0].daemon is True


def test_recheck_nvenc_updates_live_flag(monkeypatch):
    monkeypatch.setattr(encoder, "NVENC_AVAILABLE", False)
    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(0))
    assert encoder.recheck_nvenc() is True
    assert encoder.NVENC_AVAILABLE is True

    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(1))
    assert encoder.recheck_nvenc() is False
    assert encoder.NVENC_AVAILABLE is False


def test_background_retry_loop_stops_once_available(monkeypatch):
    monkeypatch.setattr(encoder, "NVENC_AVAILABLE", False)
    monkeypatch.setattr(encoder.subprocess, "run", _fake_run(0))
    sleeps = []
    monkeypatch.setattr(encoder.time, "sleep", lambda s: sleeps.append(s))

    encoder._background_retry_loop()  # returns as soon as recheck_nvenc() succeeds

    assert sleeps == [encoder._NVENC_BACKGROUND_RETRY_SECS]
    assert encoder.NVENC_AVAILABLE is True
