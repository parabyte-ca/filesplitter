"""In-app update checking and the host-upgrade-folder protocol.

Checks the configured GitHub repo for a newer release, caches the result in the
settings table, and reads/writes the shared ./upgrade folder that's the only channel
between this container and the root-owned host helper (scripts/upgrade-agent.sh).
Every read from that folder is treated as untrusted input — malformed, oversized, or
symlinked files are ignored rather than raised.
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import requests

import config
import db

logger = logging.getLogger(__name__)

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SOURCE_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")

_CACHE_KEY = "update_check_cache"
_CHECK_RATE_LIMIT_SECS = 60
_DAILY_LOOP_SECS = 86400
_MAX_NAME_CHARS = 200
_MAX_NOTES_CHARS = 20000
_MAX_ERROR_CHARS = 300
_MAX_REQUESTED_BY_CHARS = 64
_MAX_JSON_FILE_BYTES = 65536
_MAX_LOG_TAIL_BYTES = 65536

_REQUEST_STALE_SECS = 3600       # 1 hour
_RUNNING_STALE_SECS = 3 * 3600   # 3 hours


def parse_version(s) -> tuple[int, int, int] | None:
    """Strict MAJOR.MINOR.PATCH (optional leading 'v'). No pre-release/build suffixes."""
    if not isinstance(s, str):
        return None
    m = _SEMVER_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def compare_versions(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return (a > b) - (a < b)


def _owner_repo_from_source_url(url: str) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    m = _SOURCE_URL_RE.match(url.strip())
    if not m:
        return None
    return (m.group(1), m.group(2))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s) -> datetime | None:
    """Tolerant ISO-8601 parse — never raises, returns None on anything unparsable."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_latest_release(source_url: str, current_version: str, timeout: int = 10) -> dict:
    """Fetch the latest GitHub release. Raises on any failure (caller catches)."""
    owner_repo = _owner_repo_from_source_url(source_url)
    if owner_repo is None:
        raise ValueError(f"APP_SOURCE_URL is not a recognized GitHub repo URL: {source_url!r}")
    owner, repo = owner_repo

    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        headers={
            "User-Agent": f"FileSplitter/{current_version}",
            "Accept": "application/vnd.github+json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    tag = data.get("tag_name", "")
    version_tuple = parse_version(tag)
    if version_tuple is None:
        raise ValueError(f"latest release tag is not a recognized version: {tag!r}")
    version = ".".join(str(n) for n in version_tuple)

    return {
        "version": version,
        "tag": str(tag)[:_MAX_NAME_CHARS],
        "name": str(data.get("name") or "")[:_MAX_NAME_CHARS],
        "notes": str(data.get("body") or "")[:_MAX_NOTES_CHARS],
        "url": str(data.get("html_url") or ""),
        "published_at": str(data.get("published_at") or ""),
    }


def _empty_cache() -> dict:
    return {"latest": None, "checked_at": None, "last_error": None}


def _load_cache() -> dict:
    raw = db.get_setting(_CACHE_KEY, "")
    if not raw:
        return _empty_cache()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _empty_cache()
    if not isinstance(data, dict):
        return _empty_cache()
    out = _empty_cache()
    out.update({k: data.get(k) for k in out if k in data})
    return out


def _save_cache(latest: dict | None, checked_at: str, last_error: str | None) -> dict:
    cache = {
        "latest": latest,
        "checked_at": checked_at,
        "last_error": (last_error[:_MAX_ERROR_CHARS] if last_error else None),
    }
    db.set_setting(_CACHE_KEY, json.dumps(cache))
    return cache


def check_for_update(force: bool = False) -> dict:
    """Entry point for both the daily loop and the on-demand 'check now' route.

    Respects a 60s rate limit unless force=True. On failure, keeps the previously
    cached release and only updates checked_at/last_error.
    """
    cache = _load_cache()
    if not force:
        checked_at = _parse_iso(cache.get("checked_at"))
        if checked_at is not None:
            age = (datetime.now(timezone.utc) - checked_at).total_seconds()
            if 0 <= age < _CHECK_RATE_LIMIT_SECS:
                return cache

    now = _now_iso()
    try:
        latest = fetch_latest_release(config.APP_SOURCE_URL, config.VERSION)
    except Exception as exc:
        logger.error("Update check failed: %s", exc)
        return _save_cache(cache.get("latest"), now, str(exc))

    return _save_cache(latest, now, None)


def is_update_available(current_version: str, cache: dict) -> bool:
    latest = cache.get("latest")
    if not latest:
        return False
    current = parse_version(current_version)
    latest_v = parse_version(latest.get("version", ""))
    if current is None or latest_v is None:
        return False
    return compare_versions(latest_v, current) > 0


def _update_check_enabled() -> bool:
    return bool(config.APP_UPDATE_CHECK)


def daily_check_loop() -> None:
    """Mirrors app.py's _trash_cleanup_loop shape: daily, re-checks the toggle hourly."""
    while True:
        try:
            if _update_check_enabled():
                check_for_update(force=True)
                time.sleep(_DAILY_LOOP_SECS)
            else:
                time.sleep(3600)
            continue
        except Exception as exc:
            logger.error("Update check loop error: %s", exc)
        time.sleep(_DAILY_LOOP_SECS)


# ---- Shared upgrade folder I/O ----

def _upgrade_path(*parts: str) -> str:
    return os.path.join(config.APP_UPGRADE_DIR, *parts)


def _read_json_safe(path: str, max_bytes: int = _MAX_JSON_FILE_BYTES) -> dict | None:
    try:
        if os.path.islink(path):
            return None
        if not os.path.isfile(path):
            return None
        if os.path.getsize(path) > max_bytes:
            return None
        with open(path, "r", encoding="utf-8", errors="strict") as f:
            data = json.loads(f.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def get_agent_status() -> dict:
    agent = _read_json_safe(_upgrade_path("agent.json"))
    if agent is None:
        return {"configured": False, "connected": False, "last_seen": None, "every_minutes": None}

    last_seen_raw = agent.get("last_seen")
    every_minutes = agent.get("every_minutes")
    last_seen = _parse_iso(last_seen_raw)

    connected = False
    if last_seen is not None and isinstance(every_minutes, (int, float)) and every_minutes > 0:
        now = datetime.now(timezone.utc)
        threshold_secs = max(3, 2 * every_minutes + 1) * 60
        age = (now - last_seen).total_seconds()
        future_skew = (last_seen - now).total_seconds()
        # age is negative when last_seen is in the future — that's fine as long as
        # it's within the clock-skew guard below, so there's no lower bound on age.
        connected = (age <= threshold_secs) and (future_skew <= 60)

    return {
        "configured": True,
        "connected": connected,
        "last_seen": last_seen_raw if isinstance(last_seen_raw, str) else None,
        "every_minutes": every_minutes if isinstance(every_minutes, (int, float)) else None,
    }


def _valid_request(req: dict | None) -> dict | None:
    if req is None:
        return None
    rid = req.get("id")
    version = req.get("version")
    requested_at = req.get("requested_at")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return None
    if parse_version(version) is None:
        return None
    if _parse_iso(requested_at) is None:
        return None
    requested_by = req.get("requested_by")
    return {
        "id": rid,
        "version": version,
        "requested_by": str(requested_by)[:_MAX_REQUESTED_BY_CHARS] if requested_by else None,
        "requested_at": requested_at,
    }


def _valid_status(status: dict | None) -> dict | None:
    if status is None:
        return None
    rid = status.get("id")
    state = status.get("state")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return None
    if state not in ("running", "succeeded", "failed"):
        return None
    return status


def get_upgrade_state() -> dict:
    """Read-only/derived upgrade state. A present request.json always wins over
    status.json, since the helper's contract is to delete request.json the instant
    it starts work — if it still exists, nobody has picked it up yet, full stop.
    """
    base = {
        "state": "idle", "id": None, "from_version": None, "to_version": None,
        "requested_by": None, "requested_at": None, "started_at": None,
        "finished_at": None, "message": None,
    }

    req = _valid_request(_read_json_safe(_upgrade_path("request.json")))
    if req is not None:
        requested_at = _parse_iso(req["requested_at"])
        age = (datetime.now(timezone.utc) - requested_at).total_seconds()
        if age > _REQUEST_STALE_SECS:
            base.update(state="failed", id=req["id"], to_version=req["version"],
                        requested_by=req["requested_by"], requested_at=req["requested_at"],
                        message="The upgrade helper never picked this request up within an hour.")
        else:
            base.update(state="requested", id=req["id"], to_version=req["version"],
                        requested_by=req["requested_by"], requested_at=req["requested_at"])
        return base

    status = _valid_status(_read_json_safe(_upgrade_path("status.json")))
    if status is None:
        return base

    state = status.get("state")
    started_at_raw = status.get("started_at")
    finished_at_raw = status.get("finished_at")
    message = status.get("message")
    from_version = status.get("from_version")
    to_version = status.get("to_version")
    requested_by = status.get("requested_by")

    common = dict(
        id=status.get("id"),
        from_version=from_version if isinstance(from_version, str) else None,
        to_version=to_version if isinstance(to_version, str) else None,
        requested_by=str(requested_by)[:_MAX_REQUESTED_BY_CHARS] if requested_by else None,
        started_at=started_at_raw if isinstance(started_at_raw, str) else None,
        finished_at=finished_at_raw if isinstance(finished_at_raw, str) else None,
        message=str(message)[:_MAX_ERROR_CHARS] if message else None,
    )

    if state == "running":
        started_at = _parse_iso(started_at_raw)
        if started_at is not None:
            age = (datetime.now(timezone.utc) - started_at).total_seconds()
            if age > _RUNNING_STALE_SECS:
                base.update(state="failed", **common)
                base["message"] = "The upgrade helper stopped reporting progress."
                return base
        base.update(state="running", **common)
        return base

    base.update(state=state, **common)
    return base


def write_upgrade_request(version: str, requested_by: str) -> dict:
    """Atomically write request.json. Raises OSError on any failure."""
    req = {
        "id": uuid.uuid4().hex,
        "version": version,
        "requested_by": str(requested_by)[:_MAX_REQUESTED_BY_CHARS],
        "requested_at": _now_iso(),
    }
    final_path = _upgrade_path("request.json")
    tmp_path = _upgrade_path(f".request.json.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(req))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, final_path)
    return req


def read_log_tail(max_bytes: int = _MAX_LOG_TAIL_BYTES) -> str:
    path = _upgrade_path("upgrade.log")
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return ""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""
