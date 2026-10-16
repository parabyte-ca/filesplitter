import json

import pytest

import config
import updates as u


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise u.requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json_data


def test_load_cache_missing_setting(tmp_db):
    assert u._load_cache() == {"latest": None, "checked_at": None, "last_error": None}


def test_load_cache_non_json_string(tmp_db):
    import db
    db.set_setting(u._CACHE_KEY, "not json {{{")
    assert u._load_cache() == {"latest": None, "checked_at": None, "last_error": None}


def test_load_cache_json_array(tmp_db):
    import db
    db.set_setting(u._CACHE_KEY, json.dumps([1, 2, 3]))
    assert u._load_cache() == {"latest": None, "checked_at": None, "last_error": None}


def test_fetch_latest_release_non_semver_tag_raises(monkeypatch):
    monkeypatch.setattr(u.requests, "get", lambda *a, **k: _FakeResponse(
        json_data={"tag_name": "not-a-version", "body": "", "html_url": "", "published_at": ""},
    ))
    with pytest.raises(ValueError):
        u.fetch_latest_release("https://github.com/parabyte-ca/filesplitter", "1.0.0")


def test_fetch_latest_release_truncates_notes(monkeypatch):
    long_notes = "x" * 30000
    monkeypatch.setattr(u.requests, "get", lambda *a, **k: _FakeResponse(
        json_data={
            "tag_name": "v1.1.0", "name": "Release", "body": long_notes,
            "html_url": "https://example.com", "published_at": "2026-01-01T00:00:00Z",
        },
    ))
    result = u.fetch_latest_release("https://github.com/parabyte-ca/filesplitter", "1.0.0")
    assert len(result["notes"]) == u._MAX_NOTES_CHARS


def test_fetch_latest_release_sets_user_agent(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(json_data={
            "tag_name": "v1.1.0", "name": "x", "body": "y",
            "html_url": "z", "published_at": "now",
        })

    monkeypatch.setattr(u.requests, "get", fake_get)
    u.fetch_latest_release("https://github.com/parabyte-ca/filesplitter", "1.0.0")
    assert captured["headers"]["User-Agent"] == "FileSplitter/1.0.0"


def test_check_for_update_within_rate_limit_skips_network(tmp_db, monkeypatch):
    u._save_cache({"version": "1.0.0"}, u._now_iso(), None)

    def fail_if_called(*a, **k):
        raise AssertionError("requests.get should not be called within the rate-limit window")
    monkeypatch.setattr(u.requests, "get", fail_if_called)

    result = u.check_for_update(force=False)
    assert result["latest"]["version"] == "1.0.0"


def test_check_for_update_past_window_calls_network(tmp_db, monkeypatch):
    from datetime import datetime, timezone, timedelta
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    u._save_cache({"version": "1.0.0"}, old_time, None)

    monkeypatch.setattr(u.requests, "get", lambda *a, **k: _FakeResponse(json_data={
        "tag_name": "v1.1.0", "name": "x", "body": "y", "html_url": "z", "published_at": "now",
    }))
    result = u.check_for_update(force=False)
    assert result["latest"]["version"] == "1.1.0"


def test_check_for_update_network_failure_preserves_previous_latest(tmp_db, monkeypatch):
    u._save_cache({"version": "1.0.0"}, u._now_iso(), None)

    def fake_get(*a, **k):
        return _FakeResponse(status_code=500)
    monkeypatch.setattr(u.requests, "get", fake_get)

    result = u.check_for_update(force=True)
    assert result["latest"]["version"] == "1.0.0"
    assert result["last_error"] is not None


def test_check_for_update_error_capped_at_300_chars(tmp_db, monkeypatch):
    def fake_get(*a, **k):
        raise RuntimeError("x" * 500)
    monkeypatch.setattr(u.requests, "get", fake_get)

    result = u.check_for_update(force=True)
    assert len(result["last_error"]) == u._MAX_ERROR_CHARS


def test_check_for_update_force_always_calls_network(tmp_db, monkeypatch):
    u._save_cache({"version": "1.0.0"}, u._now_iso(), None)
    called = {"n": 0}

    def fake_get(*a, **k):
        called["n"] += 1
        return _FakeResponse(json_data={
            "tag_name": "v1.2.0", "name": "x", "body": "y", "html_url": "z", "published_at": "now",
        })
    monkeypatch.setattr(u.requests, "get", fake_get)

    u.check_for_update(force=True)
    assert called["n"] == 1
