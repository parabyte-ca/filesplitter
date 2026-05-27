import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import config
import db
import encoder
import splitter


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} KB"

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_paused = threading.Event()
_paused.set()  # not paused by default

_cancel_events: dict[int, threading.Event] = {}


def start(num_workers: int = None) -> None:
    global _executor
    _executor = ThreadPoolExecutor(max_workers=num_workers or config.MAX_WORKERS)
    if db.get_setting('queue_paused', '0') == '1':
        _paused.clear()
        logger.info("Queue starting in paused state (persisted)")
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


def cancel_job(job_id: int) -> bool:
    """Signal a running job to cancel. Returns True if the signal was sent."""
    event = _cancel_events.get(job_id)
    if event:
        event.set()
        logger.info("Cancel signal sent for job %d", job_id)
        return True
    return False


def _queue_loop() -> None:
    import time
    while True:
        _paused.wait()
        job = db.dequeue_next_job()
        if job:
            job_dict = dict(job)
            job_id = job_dict["id"]
            cancel_event = threading.Event()
            _cancel_events[job_id] = cancel_event  # register BEFORE submit
            _executor.submit(_run_job, job_dict, cancel_event)
        else:
            time.sleep(3)


def _run_job(job: dict, cancel_event: threading.Event) -> None:
    job_id = job["id"]
    file_id = job["file_id"]
    job_type = job["job_type"]
    target_res = job.get("target_resolution", "original")

    file_row = db.get_file(file_id)
    if file_row is None:
        logger.error("Job %d: file %d not found", job_id, file_id)
        db.set_job_status(job_id, "error")
        _cancel_events.pop(job_id, None)
        return

    # If cancel was signalled before this thread was free, bail out immediately.
    if cancel_event.is_set():
        db.set_file_status(file_id, "pending")
        db.set_job_status(job_id, "cancelled")
        db.update_job_progress(job_id, 0, "Cancelled before start")
        _cancel_events.pop(job_id, None)
        return

    input_path = file_row["path"]
    db.set_file_status(file_id, "processing")
    db.set_job_status(job_id, "running")

    # Track the last progress log so we can use it as the error message on failure.
    last_log = [""]

    def progress_cb(pct: float, log_line: str) -> None:
        last_log[0] = log_line
        db.update_job_progress(job_id, pct, log_line)

    try:
        if job_type == "encode":
            success = encoder.encode_to_x265(
                input_path,
                target_resolution=target_res,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                db.set_file_status(file_id, "pending")
                db.set_job_status(job_id, "cancelled")
                db.update_job_progress(job_id, 0, "Cancelled by user")
            elif success:
                orig_bytes = file_row["size_bytes"] or 0
                new_bytes = os.path.getsize(input_path)
                db.update_file_size(file_id, new_bytes, codec="hevc")
                saved = max(0, orig_bytes - new_bytes)
                savings_pct = (saved / orig_bytes) * 100 if orig_bytes else 0
                done_msg = (
                    f"Done — saved {savings_pct:.1f}%"
                    f" ({_fmt_bytes(orig_bytes)} → {_fmt_bytes(new_bytes)})"
                )
                db.set_file_status(file_id, "done")
                db.set_job_status(job_id, "done")
                db.update_job_progress(job_id, 100.0, done_msg, saved_bytes=saved)
                if saved > 0:
                    db.increment_saved_bytes(saved)
            else:
                err = last_log[0][:300] if last_log[0] else "Encoding failed"
                db.set_file_status(file_id, "error", err)
                db.set_job_status(job_id, "error")

        elif job_type == "split":
            scenes = splitter.split_by_scenes(
                input_path,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                db.set_file_status(file_id, "pending")
                db.set_job_status(job_id, "cancelled")
                db.update_job_progress(job_id, 0, "Cancelled by user")
            elif scenes is not None:
                db.set_file_status(file_id, "done")
                db.set_job_status(job_id, "done")
                db.update_job_progress(job_id, 100.0, f"{len(scenes)} scenes created")
            else:
                err = last_log[0][:300] if last_log[0] else "Splitting failed or no scenes found"
                db.set_file_status(file_id, "error", err)
                db.set_job_status(job_id, "error")

        else:
            logger.error("Unknown job type: %s", job_type)
            db.set_job_status(job_id, "error")

    except Exception as exc:
        logger.exception("Job %d failed: %s", job_id, exc)
        db.set_file_status(file_id, "error", str(exc)[:300])
        db.set_job_status(job_id, "error")
    finally:
        _cancel_events.pop(job_id, None)
