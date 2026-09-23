"""Âm thanh gọi UART qua queue; không mở cổng serial thứ hai tranh với nút."""
import queue
import secrets
import struct
import threading
import time
import wave
from .channels import beat, latest_get
from .uart_protocol import START, PCM, STOP, MAX_PAYLOAD

def command(queues, kind, payload, stop_event, interrupt=lambda: False, timeout=None):
    timeout = timeout or queues["config"]["stm32"]["command_timeout_s"]
    cancelled = threading.Event()
    job = {"kind": kind, "payload": payload, "reply": queue.Queue(maxsize=1),
           "cancel": cancelled, "deadline": time.monotonic()+timeout}
    try:
        queues["uart_tx_queue"].put(job, timeout=min(.2, timeout))
        while time.monotonic() < job["deadline"]:
            beat(queues, "audio")
            if stop_event.is_set() or interrupt():
                raise InterruptedError("Dừng âm thanh theo yêu cầu")
            try:
                result = job["reply"].get(timeout=.01)
            except queue.Empty:
                continue
            if result is not None:
                raise result
            return
        raise TimeoutError("Hết thời gian đợi luồng UART gửi âm thanh")
    finally:
        cancelled.set()

def play_wav(queues, path, stop_event, interrupt=lambda: False):
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Cần WAV PCM mono 16-bit 16000 Hz trước khi gửi STM32")
        samples = source.getnframes()
        if not samples:
            raise ValueError("WAV không có mẫu âm thanh")
        limit = queues["config"]["audio"]["playback_timeout_s"]
        if samples/16000 > limit:
            raise ValueError("Đoạn âm thanh dài hơn giới hạn playback_timeout_s")
        play_id = secrets.randbelow(0xffffffff)+1
        latest_get(queues["audio_done_queue"])
        started = time.monotonic()
        try:
            command(queues, STOP, b"", stop_event, interrupt)
            # DMA có thể còn phát nửa bộ đệm cũ tối đa khoảng 32 ms.
            if stop_event.wait(.04) or interrupt():
                raise InterruptedError("Đã hủy trước khi phát")
            command(queues, START, struct.pack("<II", play_id, samples), stop_event, interrupt)
            while True:
                data = source.readframes(MAX_PAYLOAD//2)
                if not data:
                    break
                if time.monotonic()-started > limit+5:
                    raise TimeoutError("Luồng PCM phát quá thời gian")
                command(queues, PCM, data, stop_event, interrupt)
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                beat(queues, "audio")
                if stop_event.is_set() or interrupt():
                    raise InterruptedError("Cảnh báo mới cần ngắt đoạn đang phát")
                try:
                    done = queues["audio_done_queue"].get(timeout=.01)
                    queues["audio_done_queue"].task_done()
                except queue.Empty:
                    continue
                if done["play_id"] == play_id and done["samples"] == samples:
                    return True
            raise TimeoutError("STM32 chưa xác nhận DMA đã phát hết mẫu âm thanh")
        except InterruptedError:
            return False
        finally:
            # Dừng phần đã gửi vào ring nếu bị ngắt hoặc lỗi giữa chừng.
            if not stop_event.is_set():
                command(queues, STOP, b"", stop_event, timeout=1)
