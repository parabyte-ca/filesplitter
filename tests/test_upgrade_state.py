import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import updates as u


def _write(upgrade_dir, name, data):
    with open(os.path.join(upgrade_dir, name), "w") as f:
        f.write(json.dumps(data) if not isinstance(data, str) else data)


# ---- _read_json_safe ----

def test_read_json_safe_missing_file(tmp_upgrade_dir):
    assert u._read_json_safe(os.path.join(tmp_upgrade_dir, "nope.json")) is None


def test_read_json_safe_symlink(tmp_upgrade_dir, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"a": 1}))
    link = os.path.join(tmp_upgrade_dir, "link.json")
    os.symlink(str(outside), link)
    assert u._read_json_safe(link) is None


def test_read_json_safe_oversized(tmp_upgrade_dir):
    path = os.path.join(tmp_upgrade_dir, "big.json")
    with open(path, "w") as f:
        f.write(json.dumps({"x": "y" * 70000}))
    assert u._read_json_safe(path) is None


def test_read_json_safe_malformed(tmp_upgrade_dir):
    path = os.path.join(tmp_upgrade_dir, "bad.json")
    with open(path, "w") as f:
        f.write("{not json")
    assert u._read_json_safe(path) is None


def test_read_json_safe_non_dict_toplevel(tmp_upgrade_dir):
    path = os.path.join(tmp_upgrade_dir, "arr.json")
    with open(path, "w") as f:
        f.write(json.dumps([1, 2, 3]))
    assert u._read_json_safe(path) is None


def test_read_json_safe_valid(tmp_upgrade_dir):
    path = os.path.join(tmp_upgrade_dir, "ok.json")
    with open(path, "w") as f:
        f.write(json.dumps({"a": 1}))
    assert u._read_json_safe(path) == {"a": 1}


# ---- get_upgrade_state ----

def test_get_upgrade_state_idle(tmp_upgrade_dir):
    assert u.get_upgrade_state()["state"] == "idle"


def test_get_upgrade_state_fresh_requested(tmp_upgrade_dir):
    _write(tmp_upgrade_dir, "request.json", {
        "id": uuid.uuid4().hex, "version": "1.1.0",
        "requested_by": "x", "requested_at": u._now_iso(),
    })
    state = u.get_upgrade_state()
    assert state["state"] == "requested"
    assert state["to_version"] == "1.1.0"


def test_get_upgrade_state_stale_requested_becomes_failed(tmp_upgrade_dir):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _write(tmp_upgrade_dir, "request.json", {
        "id": uuid.uuid4().hex, "version": "1.1.0", "requested_by": "x", "requested_at": old,
    })
    state = u.get_upgrade_state()
    assert state["state"] == "failed"
    assert "never picked" in state["message"]


def test_get_upgrade_state_fresh_running(tmp_upgrade_dir):
    _write(tmp_upgrade_dir, "status.json", {
        "id": uuid.uuid4().hex, "state": "running", "from_version": "1.0.0",
        "to_version": "1.1.0", "requested_by": "x",
        "started_at": u._now_iso(), "finished_at": None, "message": "",
    })
    assert u.get_upgrade_state()["state"] == "running"


def test_get_upgrade_state_stale_running_becomes_failed(tmp_upgrade_dir):
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    _write(tmp_upgrade_dir, "status.json", {
        "id": uuid.uuid4().hex, "state": "running", "from_version": "1.0.0",
        "to_version": "1.1.0", "requested_by": "x",
        "started_at": old, "finished_at": None, "message": "",
    })
    state = u.get_upgrade_state()
    assert state["state"] == "failed"
    assert "stopped reporting" in state["message"]


def test_get_upgrade_state_succeeded_passthrough(tmp_upgrade_dir):
    _write(tmp_upgrade_dir, "status.json", {
        "id": uuid.uuid4().hex, "state": "succeeded", "from_version": "1.0.0",
        "to_version": "1.1.0", "requested_by": "x",
        "started_at": u._now_iso(), "finished_at": u._now_iso(), "message": "Upgraded to 1.1.0",
    })
    state = u.get_upgrade_state()
    assert state["state"] == "succeeded"
    assert state["message"] == "Upgraded to 1.1.0"


def test_get_upgrade_state_failed_passthrough(tmp_upgrade_dir):
    _write(tmp_upgrade_dir, "status.json", {
        "id": uuid.uuid4().hex, "state": "failed", "from_version": "1.0.0",
        "to_version": "1.1.0", "requested_by": "x",
        "started_at": u._now_iso(), "finished_at": u._now_iso(), "message": "boom",
    })
    assert u.get_upgrade_state()["message"] == "boom"


def test_get_upgrade_state_request_wins_over_status(tmp_upgrade_dir):
    # Both files present simultaneously: request.json must win, per the helper's
    # contract of deleting request.json the instant it starts work.
    _write(tmp_upgrade_dir, "status.json", {
        "id": uuid.uuid4().hex, "state": "succeeded", "from_version": "1.0.0",
        "to_version": "1.0.5", "requested_by": "old",
        "started_at": u._now_iso(), "finished_at": u._now_iso(), "message": "stale previous run",
    })
    _write(tmp_upgrade_dir, "request.json", {
        "id": uuid.uuid4().hex, "version": "1.1.0",
        "requested_by": "new", "requested_at": u._now_iso(),
    })
    state = u.get_upgrade_state()
    assert state["state"] == "requested"
    assert state["to_version"] == "1.1.0"


def test_get_upgrade_state_malformed_fields_degrade_gracefully(tmp_upgrade_dir):
    _write(tmp_upgrade_dir, "request.json", {
        "id": "not-a-valid-id!!", "version": "1.1.0",
        "requested_by": "x", "requested_at": u._now_iso(),
    })
    # Invalid id means _valid_request returns None -> falls through to status.json -> idle.
    assert u.get_upgrade_state()["state"] == "idle"


def test_get_upgrade_state_malformed_timestamp_does_not_raise(tmp_upgrade_dir):
    _write(tmp_upgrade_dir, "request.json", {
        "id": uuid.uuid4().hex, "version": "1.1.0",
        "requested_by": "x", "requested_at": "not-a-timestamp",
    })
    # Unparsable requested_at means _valid_request returns None -> idle, no exception.
    assert u.get_upgrade_state()["state"] == "idle"


# ---- write_upgrade_request ----

def test_write_upgrade_request_atomic_no_leftover_tmp(tmp_upgrade_dir):
    u.write_upgrade_request("1.1.0", "192.168.1.5")
    files = os.listdir(tmp_upgrade_dir)
    assert "request.json" in files
    assert not any("tmp" in f for f in files)


def test_write_upgrade_request_raises_oserror_when_dir_missing(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "APP_UPGRADE_DIR", str(tmp_path / "does-not-exist"))
    import pytest
    with pytest.raises(OSError):
        u.write_upgrade_request("1.1.0", "x")
