import io
import json
import os
import sqlite3
import zipfile

import app as appmod
import db


def _client():
    appmod.app.secret_key = "test-secret"
    appmod.app.testing = True
    return appmod.app.test_client()


def test_backup_to_writes_a_readable_snapshot(tmp_db, tmp_path):
    db.set_setting("x265_crf", "30")
    dest = str(tmp_path / "snapshot.db")

    db.backup_to(dest)

    assert os.path.exists(dest)
    conn = sqlite3.connect(dest)
    row = conn.execute("SELECT value FROM settings WHERE key='x265_crf'").fetchone()
    conn.close()
    assert row == ("30",)


def test_backup_to_refuses_to_overwrite_existing_file(tmp_db, tmp_path):
    dest = str(tmp_path / "snapshot.db")
    with open(dest, "w") as f:
        f.write("not a database")

    try:
        db.backup_to(dest)
        assert False, "expected VACUUM INTO to refuse an existing target file"
    except sqlite3.DatabaseError:
        pass


def test_backup_route_returns_a_zip_with_db_and_settings(tmp_db, tmp_path, monkeypatch):
    # _settings_dict() reports the live, effective config (config.X265_CRF), not raw
    # DB rows -- db.set_setting() alone doesn't update that, only the full
    # POST /api/settings save flow does, so patch it directly here.
    monkeypatch.setattr(appmod.config, "X265_CRF", 31)
    db.set_setting("x265_crf", "31")
    client = _client()

    r = client.get("/api/backup")

    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    disposition = r.headers.get("Content-Disposition", "")
    assert "attachment" in disposition
    assert "filesplitter-backup-" in disposition

    zf = zipfile.ZipFile(io.BytesIO(r.data))
    names = set(zf.namelist())
    assert names == {"filesplitter.db", "settings.json"}

    settings = json.loads(zf.read("settings.json"))
    assert settings["x265_crf"] == 31

    extracted = tmp_path / "extracted.db"
    extracted.write_bytes(zf.read("filesplitter.db"))
    conn = sqlite3.connect(str(extracted))
    row = conn.execute("SELECT value FROM settings WHERE key='x265_crf'").fetchone()
    conn.close()
    assert row == ("31",)


def test_backup_route_does_not_leave_a_temp_db_behind(tmp_db, monkeypatch):
    real_mkstemp = appmod.tempfile.mkstemp
    captured = {}

    def spy_mkstemp(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        captured["path"] = path
        return fd, path
    monkeypatch.setattr(appmod.tempfile, "mkstemp", spy_mkstemp)

    client = _client()
    r = client.get("/api/backup")

    assert r.status_code == 200
    assert "path" in captured
    assert not os.path.exists(captured["path"])  # cleaned up, not left behind
