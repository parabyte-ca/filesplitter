"""
Sets required env vars BEFORE config.py (and anything importing it) is ever
imported, since config.py reads them once at module import time.
"""
import os
import sys
import tempfile

_TMP_ROOT = tempfile.mkdtemp(prefix="filesplitter-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_TMP_ROOT, "test.db"))
os.environ.setdefault("APP_UPGRADE_DIR", os.path.join(_TMP_ROOT, "upgrade"))
os.environ.setdefault("MEDIA_PATHS", os.path.join(_TMP_ROOT, "media"))
os.makedirs(os.environ["APP_UPGRADE_DIR"], exist_ok=True)
os.makedirs(os.environ["MEDIA_PATHS"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A fresh, initialized SQLite DB for a single test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    db.init_db()
    return db_path


@pytest.fixture
def tmp_upgrade_dir(tmp_path, monkeypatch):
    """A fresh, empty upgrade folder for a single test."""
    upgrade_dir = tmp_path / "upgrade"
    upgrade_dir.mkdir()
    monkeypatch.setattr(config, "APP_UPGRADE_DIR", str(upgrade_dir))
    return str(upgrade_dir)
