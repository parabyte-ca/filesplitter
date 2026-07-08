import logging
import os
import shutil
from datetime import date, datetime, timedelta

import config

logger = logging.getLogger(__name__)

TRASH_DIRNAME = ".trash"


def _media_root_for(path: str) -> str | None:
    """Return the MEDIA_PATHS root that contains `path`, or None."""
    for root in config.MEDIA_PATHS:
        root_norm = root.rstrip("/")
        if path == root_norm or path.startswith(root_norm + "/"):
            return root_norm
    return None


def _unique_dest(dest: str) -> str:
    """Append a numeric suffix if `dest` already exists so nothing is overwritten."""
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def move_to_trash(path: str) -> str:
    """
    Move `path` into <media_root>/.trash/<YYYY-MM-DD>/<relative-subpath>.

    The date folder groups deletions by day and is the key used by cleanup_trash().
    Returns the destination path. Raises OSError on failure.
    """
    root = _media_root_for(path)
    if root is None:
        raise OSError(f"File is not under any configured media root: {path}")

    rel = os.path.relpath(path, root)
    day = datetime.now().strftime("%Y-%m-%d")
    dest = os.path.join(root, TRASH_DIRNAME, day, rel)
    dest = _unique_dest(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(path, dest)
    logger.info("Moved to trash: %s → %s", path, dest)
    return dest


def cleanup_trash(max_age_days: int = 30) -> int:
    """
    Delete .trash/<date> folders older than max_age_days across all media roots.
    Returns the number of date folders removed.
    """
    cutoff = date.today() - timedelta(days=max_age_days)
    removed = 0
    for root in config.MEDIA_PATHS:
        trash_dir = os.path.join(root.rstrip("/"), TRASH_DIRNAME)
        if not os.path.isdir(trash_dir):
            continue
        for name in os.listdir(trash_dir):
            day_dir = os.path.join(trash_dir, name)
            if not os.path.isdir(day_dir):
                continue
            try:
                folder_date = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue  # not a date folder — leave it alone
            if folder_date < cutoff:
                try:
                    shutil.rmtree(day_dir)
                    removed += 1
                    logger.info("Purged old trash folder: %s", day_dir)
                except OSError as exc:
                    logger.error("Could not purge trash folder %s: %s", day_dir, exc)
    return removed
