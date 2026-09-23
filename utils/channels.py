"""Queue có giới hạn giúp không tích lũy ảnh và lời cảnh báo đã cũ."""
import itertools
import logging
import queue
import threading
import time

_sequence = itertools.count()

def make_queues(cfg):
    queues = {name: queue.Queue(maxsize=size) for name, size in {
        "tof_queue": 2, "stm32_queue": 32, "vision_queue": 1, "scene_queue": 2,
        "camera_requests": 2, "ocr_queue": 1, "result_queue": 8, "event_queue": 128,
        "uart_tx_queue": 2, "audio_done_queue": 8, "audio_status_queue": 1,
    }.items()}
    queues.update(config=cfg, audio_queue=queue.PriorityQueue(maxsize=32),
                  urgent_audio=threading.Event())
    return queues

def latest_put(channel, item):
    while True:
        try:
            channel.put_nowait(item)
            return
        except queue.Full:
            try:
                channel.get_nowait()
                channel.task_done()
            except queue.Empty:
                pass

def latest_get(channel, previous=None):
    while True:
        try:
            previous = channel.get_nowait()
            channel.task_done()
        except queue.Empty:
            return previous

def event(queues, kind, **data):
    latest_put(queues["event_queue"], {"kind": kind, "time": time.monotonic(), **data})

def beat(queues, worker):
    event(queues, "heartbeat", worker=worker)

def speak(queues, text, priority=5, key="", ttl=8.0, file=None):
    ident = next(_sequence)
    msg = {"text": text, "key": key or text, "id": ident,
           "expires": time.monotonic()+ttl, "priority": priority, "file": str(file) if file else None}
    item = (priority, ident, msg)
    try:
        queues["audio_queue"].put_nowait(item)
    except queue.Full:
        if priority > 1:
            return None
        # Cảnh báo khẩn được phép bỏ lời đang chờ, tránh bị kẹt sau OCR dài.
        while True:
            try:
                _, _, old = queues["audio_queue"].get_nowait()
                queues["audio_queue"].task_done()
                event(queues, "audio_cancelled", audio_id=old["id"])
            except queue.Empty:
                break
        queues["audio_queue"].put_nowait(item)
    if priority <= 1:
        queues["urgent_audio"].set()
    return ident

def guarded_run(name, module, queues, stop_event):
    try:
        module.run_thread(queues, stop_event)
    except Exception:
        logging.exception("Luồng %s gặp lỗi", name)
        event(queues, "fatal", worker=name)
        stop_event.set()
    finally:
        event(queues, "exited", worker=name)
