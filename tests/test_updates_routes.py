import json
import os
from datetime import datetime, timezone

import config
import db
import updates as u

import app as appmod


def _client():
    appmod.app.secret_key = "test-secret"
    appmod.app.testing = True
    return appmod.app.test_client()


def _write_agent_connected(upgrade_dir):
    with open(os.path.join(upgrade_dir, "agent.json"), "w") as f:
        json.dump({"last_seen": datetime.now(timezone.utc).isoformat(), "every_minutes": 15}, f)


def test_get_updates_respects_auth_gate(tmp_db, tmp_upgrade_dir, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "secret")
    client = _client()
    r = client.get("/api/updates", environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code in (302, 401, 403)
    if r.status_code == 302:
        assert "/login" in r.headers.get("Location", "")


def test_check_updates_within_window_skips_network(tmp_db, tmp_upgrade_dir, monkeypatch):
    u._save_cache({"version": "1.0.0"}, u._now_iso(), None)

    def fail_if_called(*a, **k):
        raise AssertionError("should not hit network within the rate-limit window")
    monkeypatch.setattr(u.requests, "get", fail_if_called)

    client = _client()
    r = client.post("/api/updates/check")
    assert r.status_code == 200


def test_request_upgrade_version_mismatch(tmp_db, tmp_upgrade_dir):
    client = _client()
    r = client.post("/api/updates/upgrade", json={"version": "9.9.9"})
    assert r.status_code == 400


def test_request_upgrade_not_newer(tmp_db, tmp_upgrade_dir):
    u._save_cache({"version": config.VERSION}, u._now_iso(), None)
    client = _client()
    r = client.post("/api/updates/upgrade", json={"version": config.VERSION})
    assert r.status_code == 400


def test_request_upgrade_already_requested(tmp_db, tmp_upgrade_dir):
    u._save_cache({"version": "9.9.9"}, u._now_iso(), None)
    _write_agent_connected(tmp_upgrade_dir)
    u.write_upgrade_request("9.9.9", "someone-else")

    client = _client()
    r = client.post("/api/updates/upgrade", json={"version": "9.9.9"})
    assert r.status_code == 409


def test_request_upgrade_helper_not_connected(tmp_db, tmp_upgrade_dir):
    u._save_cache({"version": "9.9.9"}, u._now_iso(), None)
    # no agent.json written -> not configured/connected
    client = _client()
    r = client.post("/api/updates/upgrade", json={"version": "9.9.9"})
    assert r.status_code == 409


def test_request_upgrade_write_failure_gives_install_hint(tmp_db, tmp_upgrade_dir, monkeypatch):
    # Helper must be connected (agent.json readable) for the route to even reach the
    # write step. The write itself can fail for reasons unrelated to the read path
    # (disk full, a race where the folder disappears, etc.) — simulate that directly
    # rather than via filesystem permissions, which root bypasses (as this suite may
    # run as root, e.g. inside a container).
    _write_agent_connected(tmp_upgrade_dir)
    u._save_cache({"version": "9.9.9"}, u._now_iso(), None)

    def fail_write(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(u, "write_upgrade_request", fail_write)

    client = _client()
    r = client.post("/api/updates/upgrade", json={"version": "9.9.9"})
    assert r.status_code == 500
    assert "--install" in r.get_json()["msg"]


def test_request_upgrade_success_writes_request_atomically_and_logs(
    tmp_db, tmp_upgrade_dir, caplog,
):
    u._save_cache({"version": "9.9.9"}, u._now_iso(), None)
    _write_agent_connected(tmp_upgrade_dir)

    client = _client()
    with caplog.at_level("INFO"):
        r = client.post("/api/updates/upgrade", json={"version": "9.9.9"})

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True

    files = os.listdir(tmp_upgrade_dir)
    assert "request.json" in files
    assert not any("tmp" in f for f in files)

    with open(os.path.join(tmp_upgrade_dir, "request.json")) as f:
        req = json.load(f)
    assert req["version"] == "9.9.9"

    assert any("Upgrade requested" in rec.message for rec in caplog.records)
