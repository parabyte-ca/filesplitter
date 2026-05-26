import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import config
import db
import encoder
import splitter

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_paused = threading.Event()
_paused.set()  # not paused by default

_queue_running = False


def start(num_workers: int = None) -> None:
    global _executor
    _executor = ThreadPoolExecutor(max_workers=num_workers or config.MAX_WORKERS)
    t = threading.Thread(target=_queue_loop, daemon=True)
    t.start()
    logger.info("Worker started with %d thread(s)", num_workers or config.MAX_WORKERS)


def pause() -> None:
    _paused.clear()
    logger.info("Queue paused")


def resume() -> None:
    _paused.set()
    logger.info("Queue resumed")


def is_paused() -> bool:
    return not _paused.is_set()


def _queue_loop() -> None:
    import time
    while True:
        _paused.wait()
        job = db.dequeue_next_job()
        if job:
            _executor.submit(_run_job, dict(job))
        else:
            time.sleep(3)


def _run_job(job: dict) -> None:
    job_id = job["id"]
    file_id = job["file_id"]
    job_type = job["job_type"]
    target_res = job.get("target_resolution", "original")

    file_row = db.get_file(file_id)
    if file_row is None:
        logger.error("Job %d: file %d not found", job_id, file_id)
        db.set_job_status(job_id, "error")
        return

    input_path = file_row["path"]
    db.set_file_status(file_id, "processing")
    db.set_job_status(job_id, "running")

    def progress_cb(pct: float, log_line: str) -> None:
        db.update_job_progress(job_id, pct, log_line)

    try:
        if job_type == "encode":
            success = encoder.encode_to_x265(
                input_path,
                target_resolution=target_res,
                progress_cb=progress_cb,
            )
            if success:
                db.set_file_status(file_id, "done")
                db.set_job_status(job_id, "done")
                db.update_job_progress(job_id, 100.0, "Encoding complete")
            else:
                db.set_file_status(file_id, "error", "Encoding failed")
                db.set_job_status(job_id, "error")

        elif job_type == "split":
            scenes = splitter.split_by_scenes(input_path, progress_cb=progress_cb)
            if scenes is not None:
                db.set_file_status(file_id, "done")
                db.set_job_status(job_id, "done")
                db.update_job_progress(job_id, 100.0, f"{len(scenes)} scenes created")
            else:
                db.set_file_status(file_id, "error", "Splitting failed or no scenes")
                db.set_job_status(job_id, "error")

        else:
            logger.error("Unknown job type: %s", job_type)
            db.set_job_status(job_id, "error")

    except Exception as exc:
        logger.exception("Job %d failed: %s", job_id, exc)
        db.set_file_status(file_id, "error", str(exc))
        db.set_job_status(job_id, "error")
