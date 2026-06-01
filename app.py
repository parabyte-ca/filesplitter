import hmac
import ipaddress
import json
import logging
import time
import threading
from pathlib import Path

from flask import (Flask, jsonify, render_template, render_template_string,
                   request, Response, redirect, session, stream_with_context)

import config
import db
import scanner
import worker

_VERSION = (Path(__file__).parent / "VERSION").read_text().strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=86400 * 7,
)

_LOGIN_TEMPLATE = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>FileSplitter — Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0f0f13;color:#e0e0e0;font-family:system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#1a1a24;border:1px solid #2a2a3a;border-radius:12px;padding:32px;
        width:100%;max-width:360px}
  h2{font-size:20px;margin-bottom:4px;color:#fff}
  .sub{font-size:12px;color:#888;margin-bottom:24px}
  label{display:block;font-size:12px;color:#aaa;margin-bottom:6px}
  input[type=password]{width:100%;padding:10px 12px;background:#0f0f13;border:1px solid #2a2a3a;
    border-radius:8px;color:#e0e0e0;font-size:14px;outline:none}
  input[type=password]:focus{border-color:#6c63ff}
  .err{color:#ff6b6b;font-size:12px;margin-top:8px}
  button{margin-top:16px;width:100%;padding:10px;background:#6c63ff;border:none;
    border-radius:8px;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#7c73ff}
</style></head>
<body><div class="card">
  <h2>FileSplitter</h2>
  <div class="sub">External access — authentication required</div>
  <form method="post">
    <label for="pw">Password</label>
    <input id="pw" type="password" name="password" autofocus autocomplete="current-password">
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <button type="submit">Sign in</button>
  </form>
</div></body></html>"""


_LAN_NETS = [
    ipaddress.ip_network(n) for n in [
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "::1/128", "fc00::/7",
    ]
]


def _is_lan(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _LAN_NETS)
    except ValueError:
        return False


def _requires_auth() -> bool:
    """Require auth when DASHBOARD_PASSWORD is set and the request is not from a LAN address."""
    if not config.DASHBOARD_PASSWORD:
        return False
    return not _is_lan(request.remote_addr or "")


@app.before_request
def check_auth():
    if not _requires_auth():
        return
    if request.endpoint in ("login", "login_post", "logout"):
        return
    if session.get("authenticated"):
        return
    return redirect("/login")


@app.get("/login")
def login():
    return render_template_string(_LOGIN_TEMPLATE, error=None)


@app.post("/login")
def login_post():
    if hmac.compare_digest(
        request.form.get("password", ""),
        config.DASHBOARD_PASSWORD,
    ):
        session["authenticated"] = True
        return redirect("/")
    time.sleep(1)  # slow brute-force attempts
    return render_template_string(_LOGIN_TEMPLATE, error="Incorrect password"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


_scan_lock = threading.Lock()
_scan_status = {"running": False, "last_result": None}


# --- SSE ---

def _sse_stream():
    tick = 0
    while True:
        if request.environ.get("werkzeug.is_closed") or request.environ.get("wsgi.input_terminated"):
            break
        data = {
            "stats": db.get_stats(),
            "active_jobs": [
                {
                    "id": j["id"],
                    "file": j["filename"],
                    "type": j["job_type"],
                    "progress": round(j["progress_pct"] or 0, 1),
                    "log": j["log_tail"] or "",
                    "status": j["status"],
                    "started_at": j["started_at"] or "",
                }
                for j in db.get_active_jobs()
            ],
            "paused": worker.is_paused(),
            "scan_running": _scan_status["running"],
            "version": _VERSION,
        }
        yield f"data: {json.dumps(data)}\n\n"
        tick += 1
        # Send a SSE comment every ~30s to keep Cloudflare Tunnel from dropping
        # idle connections (CF cuts connections with no traffic after ~100s).
        if tick % 15 == 0:
            yield ": keepalive\n\n"
        time.sleep(2)


@app.get("/stream")
def stream():
    return Response(
        stream_with_context(_sse_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Pages ---

@app.get("/")
def index():
    return render_template(
        "index.html",
        media_paths=config.MEDIA_PATHS,
        flask_port=config.FLASK_PORT,
        version=_VERSION,
        show_logout=bool(config.DASHBOARD_PASSWORD),
    )


# --- API ---

@app.get("/api/files")
def api_files():
    rows = db.get_all_files()
    return jsonify([dict(r) for r in rows])


@app.get("/api/jobs")
def api_jobs():
    rows = db.get_recent_jobs(50)
    return jsonify([dict(r) for r in rows])


@app.get("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.post("/api/scan")
def api_scan():
    if _scan_status["running"]:
        return jsonify({"ok": False, "msg": "Scan already running"}), 409

    def _run():
        _scan_status["running"] = True
        try:
            result = scanner.scan_all()
            _scan_status["last_result"] = result
        finally:
            _scan_status["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Scan started"})


@app.post("/api/queue")
def api_queue():
    body = request.get_json(force=True) or {}
    file_id = body.get("file_id")
    job_type = body.get("job_type")  # encode | split
    target_resolution = body.get("target_resolution", "original")

    if not file_id or job_type not in ("encode", "split"):
        return jsonify({"ok": False, "msg": "Invalid parameters"}), 400

    file_row = db.get_file(file_id)
    if file_row is None:
        return jsonify({"ok": False, "msg": "File not found"}), 404

    job_id = db.create_job(file_id, job_type, target_resolution)
    db.set_file_status(file_id, "queued")
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/api/skip/<int:file_id>")
def api_skip(file_id: int):
    file_row = db.get_file(file_id)
    if file_row is None:
        return jsonify({"ok": False, "msg": "File not found"}), 404
    if file_row["status"] in ("processing", "queued"):
        return jsonify({"ok": False, "msg": "Cannot skip a file that is queued or processing"}), 409
    db.set_file_status(file_id, "skipped")
    return jsonify({"ok": True})


@app.get("/api/version")
def api_version():
    return jsonify({"version": _VERSION})


def _cancel_job_by_id(job_id: int):
    """Shared cancel logic used by both cancel endpoints."""
    job = db.get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "msg": "Job not found"}), 404
    status = job["status"]
    if status == "running":
        worker.cancel_job(job_id)
        return jsonify({"ok": True, "msg": "Cancel signal sent — job will stop shortly"})
    if status == "queued":
        db.set_job_status(job_id, "cancelled")
        db.set_file_status(job["file_id"], "pending")
        return jsonify({"ok": True, "msg": "Queued job cancelled"})
    return jsonify({"ok": False, "msg": f"Job is '{status}', cannot cancel"}), 409


@app.post("/api/jobs/<int:job_id>/cancel")
def api_cancel_job(job_id: int):
    return _cancel_job_by_id(job_id)


@app.post("/api/files/<int:file_id>/cancel")
def api_cancel_file(file_id: int):
    job = db.get_active_job_for_file(file_id)
    if job is None:
        return jsonify({"ok": False, "msg": "No active job for this file"}), 404
    return _cancel_job_by_id(job["id"])


@app.post("/api/files/purge")
def api_purge_missing():
    count = db.purge_missing_files()
    return jsonify({"ok": True, "removed": count})


@app.post("/api/jobs/clear")
def api_clear_jobs():
    count = db.clear_finished_jobs()
    return jsonify({"ok": True, "cleared": count})


@app.post("/api/queue/pause")
def api_pause():
    worker.pause()
    db.set_setting('queue_paused', '1')
    return jsonify({"ok": True, "paused": True})


@app.post("/api/queue/resume")
def api_resume():
    worker.resume()
    db.set_setting('queue_paused', '0')
    return jsonify({"ok": True, "paused": False})


@app.get("/api/settings")
def api_get_settings():
    import encoder as _enc
    return jsonify({
        "media_paths": config.MEDIA_PATHS,
        "scene_threshold": config.SCENE_THRESHOLD,
        "min_scene_duration": config.MIN_SCENE_DURATION,
        "split_min_duration": config.SPLIT_MIN_DURATION,
        "split_min_size": config.SPLIT_MIN_SIZE,
        "split_keywords": config.SPLIT_KEYWORDS,
        "x265_crf": config.X265_CRF,
        "x265_preset": config.X265_PRESET,
        "target_resolution": config.TARGET_RESOLUTION,
        "max_workers": config.MAX_WORKERS,
        "resolutions": ["original"] + list(config.RESOLUTION_MAP.keys()),
        "flask_port": config.FLASK_PORT,
        "encoder_backend": config.ENCODER_BACKEND,
        "nvenc_available": _enc.NVENC_AVAILABLE,
        "cpu_presets": _enc._CPU_PRESETS,
        "nvenc_presets": _enc._NVENC_PRESETS,
        "scene_method": config.SCENE_METHOD,
        "black_min_duration": config.BLACK_MIN_DURATION,
        "black_pix_th": config.BLACK_PIX_TH,
        "split_scene_method": config.SPLIT_SCENE_METHOD,
        "split_min_episode_gap": config.SPLIT_MIN_EPISODE_GAP,
        "split_black_min_duration": config.SPLIT_BLACK_MIN_DURATION,
        "split_episode_count": config.SPLIT_EPISODE_COUNT,
    })


@app.post("/api/settings/saved-bytes")
def api_set_saved_bytes():
    body = request.get_json(force=True) or {}
    value = int(body.get("value", 0))
    db.set_saved_bytes(value)
    return jsonify({"ok": True, "total_saved_bytes": value})


@app.post("/api/settings")
def api_post_settings():
    """Persist runtime-adjustable settings to DB (config module re-reads on access for new jobs)."""
    body = request.get_json(force=True) or {}
    updatable = [
        "scene_threshold", "min_scene_duration", "split_min_duration",
        "split_min_size", "x265_crf", "x265_preset", "target_resolution",
        "encoder_backend", "scene_method", "black_min_duration", "black_pix_th",
        "split_scene_method", "split_min_episode_gap", "split_black_min_duration",
        "split_episode_count",
    ]
    for key in updatable:
        if key in body:
            val = body[key]
            db.set_setting(key, str(val))
            if hasattr(config, key.upper()):
                try:
                    attr = getattr(config, key.upper())
                    if isinstance(attr, int):
                        setattr(config, key.upper(), int(val))
                    elif isinstance(attr, float):
                        setattr(config, key.upper(), float(val))
                    else:
                        setattr(config, key.upper(), str(val))
                except (ValueError, AttributeError):
                    pass
    return jsonify({"ok": True})


# Settings that can be overridden at runtime via the UI and persisted to the DB.
# On startup these are re-applied so container restarts don't lose UI changes.
_SETTINGS_MAP: dict[str, tuple[str, type]] = {
    "scene_threshold":    ("SCENE_THRESHOLD",    float),
    "min_scene_duration": ("MIN_SCENE_DURATION",  int),
    "scene_method":       ("SCENE_METHOD",        str),
    "x265_crf":           ("X265_CRF",            int),
    "x265_preset":        ("X265_PRESET",         str),
    "target_resolution":  ("TARGET_RESOLUTION",   str),
    "encoder_backend":    ("ENCODER_BACKEND",     str),
    "max_workers":        ("MAX_WORKERS",         int),
    "black_min_duration": ("BLACK_MIN_DURATION",  float),
    "black_pix_th":       ("BLACK_PIX_TH",        float),
    "split_min_duration":        ("SPLIT_MIN_DURATION",        int),
    "split_min_size":            ("SPLIT_MIN_SIZE",            int),
    "split_scene_method":        ("SPLIT_SCENE_METHOD",        str),
    "split_min_episode_gap":     ("SPLIT_MIN_EPISODE_GAP",     int),
    "split_black_min_duration":  ("SPLIT_BLACK_MIN_DURATION",  float),
    "split_episode_count":       ("SPLIT_EPISODE_COUNT",       int),
}


def _reload_settings_from_db() -> None:
    """Apply persisted UI settings to config globals so they survive container restarts."""
    for db_key, (attr, cast) in _SETTINGS_MAP.items():
        val = db.get_setting(db_key)
        if val:
            try:
                setattr(config, attr, cast(val))
            except (ValueError, TypeError):
                pass


if __name__ == "__main__":
    db.init_db()
    app.secret_key = config.FLASK_SECRET_KEY or db.get_setting("secret_key")
    _reload_settings_from_db()
    worker.start()
    logger.info("FileSplitter running on port %d", config.FLASK_PORT)
    app.run(host="0.0.0.0", port=config.FLASK_PORT, threaded=True)
