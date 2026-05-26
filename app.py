import json
import logging
import time
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

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

_scan_lock = threading.Lock()
_scan_status = {"running": False, "last_result": None}


# --- SSE ---

def _sse_stream():
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
                }
                for j in db.get_active_jobs()
            ],
            "paused": worker.is_paused(),
            "scan_running": _scan_status["running"],
            "version": _VERSION,
        }
        yield f"data: {json.dumps(data)}\n\n"
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
    body = request.get_json(force=True)
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


@app.post("/api/queue/pause")
def api_pause():
    worker.pause()
    return jsonify({"ok": True, "paused": True})


@app.post("/api/queue/resume")
def api_resume():
    worker.resume()
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
    })


@app.post("/api/settings")
def api_post_settings():
    """Persist runtime-adjustable settings to DB (config module re-reads on access for new jobs)."""
    body = request.get_json(force=True)
    updatable = [
        "scene_threshold", "min_scene_duration", "split_min_duration",
        "split_min_size", "x265_crf", "x265_preset", "target_resolution",
        "encoder_backend",
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


if __name__ == "__main__":
    db.init_db()
    worker.start()
    logger.info("FileSplitter running on port %d", config.FLASK_PORT)
    app.run(host="0.0.0.0", port=config.FLASK_PORT, threaded=True)
