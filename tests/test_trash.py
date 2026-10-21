import os

import trash


def _put(media_root, day, rel_name, content=b"x"):
    day_dir = os.path.join(media_root, ".trash", day)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, rel_name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_trash_stats_empty_when_no_trash_dirs(tmp_media_roots):
    assert trash.trash_stats() == {"size_bytes": 0, "file_count": 0}


def test_trash_stats_sums_across_all_media_roots(tmp_media_roots):
    root1, root2 = tmp_media_roots
    _put(root1, "2026-01-01", "a.mp4", b"12345")
    _put(root1, "2026-10-21", "b.mp4", b"1234567890")  # today's date folder counts too
    _put(root2, "2026-05-01", "c.mp4", b"12")
    stats = trash.trash_stats()
    assert stats == {"size_bytes": 17, "file_count": 3}


def test_trash_stats_ignores_missing_file_mid_walk(tmp_media_roots, monkeypatch):
    # A file that vanishes between os.walk()'s listing and getsize() is skipped
    # entirely (neither sized nor counted) rather than partially credited.
    root1, _ = tmp_media_roots
    _put(root1, "2026-01-01", "a.mp4", b"12345")

    def flaky_getsize(path):
        raise OSError("vanished")
    monkeypatch.setattr(os.path, "getsize", flaky_getsize)

    stats = trash.trash_stats()
    assert stats == {"size_bytes": 0, "file_count": 0}


def test_empty_trash_removes_everything_including_todays_folder(tmp_media_roots):
    root1, root2 = tmp_media_roots
    _put(root1, "2026-01-01", "old.mp4", b"12345")
    _put(root1, "2026-10-21", "today.mp4", b"1234567890")  # cleanup_trash would never touch this
    _put(root2, "2026-05-01", "other.mp4", b"12")

    result = trash.empty_trash()
    assert result == {"size_bytes": 17, "file_count": 3}
    assert trash.trash_stats() == {"size_bytes": 0, "file_count": 0}

    # .trash dirs themselves are left in place (just emptied), not removed
    assert os.path.isdir(os.path.join(root1, ".trash"))
    assert os.listdir(os.path.join(root1, ".trash")) == []


def test_empty_trash_no_trash_dirs_is_a_noop(tmp_media_roots):
    assert trash.empty_trash() == {"size_bytes": 0, "file_count": 0}


def test_empty_trash_one_bad_entry_does_not_abort_the_rest(tmp_media_roots, monkeypatch):
    root1, root2 = tmp_media_roots
    _put(root1, "2026-01-01", "a.mp4", b"12345")
    _put(root2, "2026-05-01", "b.mp4", b"12")

    real_rmtree = trash.shutil.rmtree

    def flaky_rmtree(path, *a, **k):
        if "2026-01-01" in path:
            raise OSError("permission denied")
        return real_rmtree(path, *a, **k)
    monkeypatch.setattr(trash.shutil, "rmtree", flaky_rmtree)

    trash.empty_trash()
    # the one that failed is still there; the other media root's trash was still cleared
    assert os.path.isdir(os.path.join(root1, ".trash", "2026-01-01"))
    assert trash.trash_stats() == {"size_bytes": 5, "file_count": 1}
