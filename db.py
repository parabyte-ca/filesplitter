import re
import sqlite3
import os
from datetime import datetime, timezone
from contextlib import contextmanager

import config

_LOG_SAVINGS_RE = re.compile(r'\(([0-9.]+) (GB|MB|KB) → ([0-9.]+) (GB|MB|KB)\)')


def _parse_bytes_log(val: str, unit: str) -> int:
    n = float(val)
    if unit == 'GB': return int(n * 1_000_000_000)
    if unit == 'MB': return int(n * 1_000_000)
    return int(n * 1_000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    os.makedirs(os.path.dirname(config.DATABASE_PATH), exist_ok=True)
    # WAL must be set outside executescript (which auto-commits)
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            size_bytes INTEGER,
            duration_sec REAL,
            codec TEXT,
            is_anthology INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            error_msg TEXT,
            discovered_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER REFERENCES files(id),
            job_type TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            progress_pct REAL DEFAULT 0,
            target_resolution TEXT DEFAULT 'original',
            log_tail TEXT,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_file_id ON jobs(file_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    """)
    # Add columns introduced after the initial schema — try/except handles
    # both old SQLite (no IF NOT EXISTS on ALTER TABLE) and existing DBs
    for _ddl in [
        "ALTER TABLE files ADD COLUMN width INTEGER DEFAULT 0",
        "ALTER TABLE files ADD COLUMN height INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN saved_bytes INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(_ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    # Backfill saved_bytes from log_tail for encode jobs that pre-date v0.9.0
    rows = conn.execute(
        "SELECT id, log_tail FROM jobs"
        " WHERE status='done' AND job_type='encode' AND saved_bytes=0 AND log_tail IS NOT NULL"
    ).fetchall()
    for row in rows:
        m = _LOG_SAVINGS_RE.search(row[1])
        if m:
            orig = _parse_bytes_log(m.group(1), m.group(2))
            new  = _parse_bytes_log(m.group(3), m.group(4))
            saved = max(0, orig - new)
            if saved > 0:
                conn.execute("UPDATE jobs SET saved_bytes=? WHERE id=?", (saved, row[0]))
    conn.commit()
    # Fix files whose codec wasn't updated at encode time (pre-v0.9.2):
    # any file with a done encode job should be recorded as hevc
    conn.execute("""
        UPDATE files SET codec='hevc'
        WHERE codec != 'hevc'
          AND id IN (
              SELECT DISTINCT file_id FROM jobs
              WHERE job_type='encode' AND status='done'
          )
    """)
    conn.commit()
    # Reset jobs left in running/processing state by a previous container crash
    conn.execute(
        "UPDATE jobs SET status='error', log_tail='Interrupted by restart', finished_at=?"
        " WHERE status='running'",
        (_now(),),
    )
    conn.execute("UPDATE files SET status='pending' WHERE status='processing'")
    conn.commit()
    conn.close()


@contextmanager
def connect():
    conn = sqlite3.connect(config.DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Files ---

def upsert_file(path: str, filename: str, size_bytes: int, duration_sec: float,
                codec: str, is_anthology: bool, width: int = 0, height: int = 0) -> int:
    now = _now()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, status FROM files WHERE path = ?", (path,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE files SET filename=?, size_bytes=?, duration_sec=?,
                   codec=?, is_anthology=?, width=?, height=?, updated_at=? WHERE path=?""",
                (filename, size_bytes, duration_sec, codec, int(is_anthology), width, height, now, path),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO files (path, filename, size_bytes, duration_sec,
                   codec, is_anthology, width, height, status, discovered_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (path, filename, size_bytes, duration_sec, codec, int(is_anthology), width, height, now, now),
            )
            return cur.lastrowid


def set_file_status(file_id: int, status: str, error_msg: str = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE files SET status=?, error_msg=?, updated_at=? WHERE id=?",
            (status, error_msg, _now(), file_id),
        )


def get_file_by_path(path: str) -> sqlite3.Row:
    with connect() as conn:
        return conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()


def get_file(file_id: int) -> sqlite3.Row:
    with connect() as conn:
        return conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()


def get_active_job_for_file(file_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE file_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
            (file_id,),
        ).fetchone()


def get_job(job_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT j.*, f.filename, f.path FROM jobs j JOIN files f ON j.file_id = f.id WHERE j.id=?",
            (job_id,),
        ).fetchone()


def get_all_files() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM files ORDER BY updated_at DESC"
        ).fetchall()


def get_stats() -> dict:
    with connect() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) as processing,
                SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) as queued,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors,
                SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) as skipped,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
            FROM files
        """).fetchone()
        saved = conn.execute(
            "SELECT COALESCE(SUM(saved_bytes), 0) as total_saved_bytes"
            " FROM jobs WHERE status='done' AND job_type='encode'"
        ).fetchone()
        result = dict(row) if row else {}
        result["total_saved_bytes"] = saved["total_saved_bytes"] if saved else 0
        return result


# --- Jobs ---

def create_job(file_id: int, job_type: str, target_resolution: str = "original") -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO jobs (file_id, job_type, status, target_resolution)
               VALUES (?, ?, 'queued', ?)""",
            (file_id, job_type, target_resolution),
        )
        return cur.lastrowid


def get_active_jobs() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("""
            SELECT j.*, f.filename, f.path, f.codec, f.size_bytes
            FROM jobs j JOIN files f ON j.file_id = f.id
            WHERE j.status IN ('queued', 'running')
            ORDER BY j.id
        """).fetchall()


def purge_missing_files() -> int:
    """Delete records for files whose path no longer exists on disk. Returns count removed."""
    with connect() as conn:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        missing_ids = [r["id"] for r in rows if not os.path.exists(r["path"])]
        if not missing_ids:
            return 0
        placeholders = ",".join("?" * len(missing_ids))
        conn.execute(f"DELETE FROM jobs WHERE file_id IN ({placeholders})", missing_ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", missing_ids)
        return len(missing_ids)


def clear_finished_jobs() -> int:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done','error','cancelled')"
        )
        return cur.rowcount


def get_recent_jobs(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("""
            SELECT j.*, f.filename, f.path
            FROM jobs j JOIN files f ON j.file_id = f.id
            ORDER BY j.id DESC LIMIT ?
        """, (limit,)).fetchall()


def update_file_size(file_id: int, size_bytes: int, codec: str = None) -> None:
    with connect() as conn:
        if codec:
            conn.execute(
                "UPDATE files SET size_bytes=?, codec=?, updated_at=? WHERE id=?",
                (size_bytes, codec, _now(), file_id),
            )
        else:
            conn.execute(
                "UPDATE files SET size_bytes=?, updated_at=? WHERE id=?",
                (size_bytes, _now(), file_id),
            )


def update_job_progress(job_id: int, progress_pct: float, log_tail: str = None,
                        saved_bytes: int = None) -> None:
    with connect() as conn:
        if saved_bytes is not None:
            conn.execute(
                "UPDATE jobs SET progress_pct=?, log_tail=?, saved_bytes=? WHERE id=?",
                (progress_pct, log_tail, saved_bytes, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET progress_pct=?, log_tail=? WHERE id=?",
                (progress_pct, log_tail, job_id),
            )


def set_job_status(job_id: int, status: str) -> None:
    now = _now()
    with connect() as conn:
        if status == "running":
            conn.execute(
                "UPDATE jobs SET status=?, started_at=? WHERE id=?",
                (status, now, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status=?, finished_at=? WHERE id=?",
                (status, now, job_id),
            )


def dequeue_next_job() -> sqlite3.Row | None:
    """Atomically claim the next queued job. Safe for concurrent callers."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        # Conditional UPDATE guards against concurrent dequeue — only one
        # thread's UPDATE will find status='queued' and win.
        updated = conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (_now(), row["id"]),
        ).rowcount
        if updated == 0:
            return None
        return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


# --- Settings ---

def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
