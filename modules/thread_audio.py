"""Piper tạo WAV rồi truyền PCM qua UART tới jack STM32; cảnh báo khẩn ngắt OCR."""
from collections import OrderedDict
from contextlib import nullcontext
import hashlib
import logging
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
from ..utils.channels import beat, event
from ..utils.text_postprocess import speech_chunks
from ..config import ROOT
from ..utils.uart_audio import play_wav

def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

def should_interrupt(queues, priority):
    channel = queues["audio_queue"]
    now = time.monotonic()
    with channel.mutex:
        return any(p < priority and p <= 1 and item["expires"] >= now
                   for p, _, item in channel.queue)
def run_process(args, queues, stop_event, priority, timeout):
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    deadline = time.monotonic()+timeout
    try:
        while process.poll() is None:
            beat(queues, "audio")
            if stop_event.is_set() or should_interrupt(queues, priority):
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError("Tổng hợp hoặc phát âm thanh quá thời gian")
            stop_event.wait(0.03)
        if process.returncode != 0:
            raise RuntimeError(f"Tiến trình âm thanh thoát với mã {process.returncode}")
        return True
    finally:
        stop_process(process)

def run_thread(queues, stop_event):
    cfg, mock = queues["config"]["audio"], queues["config"]["runtime"]["mock"]
    cache = OrderedDict()
    recent = {}
    event(queues, "ready", worker="audio")
    cache_root = ROOT/".cache"/"audio"
    if not mock:
        cache_root.mkdir(parents=True, exist_ok=True)
    context = nullcontext("") if mock else tempfile.TemporaryDirectory(prefix="session_", dir=cache_root)
    with context as folder:
        while not stop_event.is_set():
            beat(queues, "audio")
            try:
                priority, _, item = queues["audio_queue"].get(timeout=0.05)
            except queue.Empty:
                queues["urgent_audio"].clear()
                continue
            finished = False
            try:
                now = time.monotonic()
                if now > item["expires"] or now-recent.get(item["key"], -999) < 1.0:
                    continue
                event(queues, "audio_started", audio_id=item["id"])
                if priority <= 1:
                    queues["urgent_audio"].clear()
                if mock:
                    logging.info("Âm thanh mô phỏng: %s", item["text"])
                    finished = not stop_event.wait(0.03)
                else:
                    finished = True
                    chunks = [None] if item.get("file") else speech_chunks(item["text"])
                    for text in chunks:
                        if stop_event.is_set() or should_interrupt(queues, priority):
                            finished = False
                            break
                        source_file = Path(item["file"]) if item.get("file") else None
                        cache_key = f"{source_file.resolve()}:{source_file.stat().st_mtime_ns}" if source_file else text
                        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
                        wav = Path(folder)/(digest+".wav")
                        if digest not in cache:
                            if source_file is None:
                                text_path = Path(folder)/"request.txt"
                                text_path.write_text(text, encoding="utf-8")
                                source_file = Path(folder)/"piper_source.wav"
                                args = [sys.executable, str(Path(__file__).with_name("piper_synth.py")),
                                        "--model", cfg["model"], "--text", str(text_path), "--wav", str(source_file)]
                                if not run_process(args, queues, stop_event, priority, cfg["synthesis_timeout_s"]):
                                    finished = False
                                    break
                            args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source_file),
                                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
                            if not run_process(args, queues, stop_event, priority, cfg["synthesis_timeout_s"]):
                                finished = False
                                wav.unlink(missing_ok=True)
                                break
                            cache[digest] = wav
                            while len(cache) > cfg["cache_entries"]:
                                _, old = cache.popitem(last=False)
                                old.unlink(missing_ok=True)
                        cache.move_to_end(digest)
                        if not play_wav(queues, wav, stop_event, lambda: should_interrupt(queues, priority)):
                            finished = False
                            break
                if finished:
                    recent[item["key"]] = time.monotonic()
                    if len(recent) > 128:
                        recent.pop(next(iter(recent)))
            finally:
                event(queues, "audio_done" if finished else "audio_cancelled", audio_id=item["id"])
                queues["audio_queue"].task_done()
