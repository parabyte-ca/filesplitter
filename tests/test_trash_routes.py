import os

import app as appmod


def _client():
    appmod.app.secret_key = "test-secret"
    appmod.app.testing = True
    return appmod.app.test_client()


def _put(media_root, day, rel_name, content=b"x"):
    day_dir = os.path.join(media_root, ".trash", day)
    os.makedirs(day_dir, exist_ok=True)
    with open(os.path.join(day_dir, rel_name), "wb") as f:
        f.write(content)


def test_get_trash_empty(tmp_db, tmp_media_roots):
    client = _client()
    r = client.get("/api/trash")
    assert r.status_code == 200
    assert r.get_json() == {"size_bytes": 0, "file_count": 0}


def test_get_trash_reports_contents(tmp_db, tmp_media_roots):
    root1, _ = tmp_media_roots
    _put(root1, "2026-01-01", "a.mp4", b"12345")
    client = _client()
    r = client.get("/api/trash")
    assert r.get_json() == {"size_bytes": 5, "file_count": 1}


def test_empty_trash_route_clears_and_reports_freed(tmp_db, tmp_media_roots):
    root1, root2 = tmp_media_roots
    _put(root1, "2026-01-01", "a.mp4", b"12345")
    _put(root2, "2026-10-21", "b.mp4", b"12")

    client = _client()
    r = client.post("/api/trash/empty")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["freed_bytes"] == 7
    assert body["removed_files"] == 2

    r2 = client.get("/api/trash")
    assert r2.get_json() == {"size_bytes": 0, "file_count": 0}
