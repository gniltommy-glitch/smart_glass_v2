"""Kiểm tra luồng nền, mất ACK, ngắt âm thanh và thoát sạch bằng thiết bị giả."""
import subprocess
import sys
import threading
import time
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from smart_glass_v2.config import load_config
from smart_glass_v2.main_coordinator import run
from smart_glass_v2.modules import thread_stm32, thread_audio
from smart_glass_v2.utils.channels import make_queues, speak, guarded_run
from smart_glass_v2.utils.uart_protocol import encode, feed, new_decoder, BUTTON, BUTTON_ACK, BOOT

class WorkerTests(unittest.TestCase):
    def test_priority_zero_preempts_one_but_expired_warning_does_not(self):
        queues = make_queues(load_config(mock=True))
        speak(queues, "Cảnh báo đã cũ", 0, ttl=-1)
        self.assertFalse(thread_audio.should_interrupt(queues, 2))
        speak(queues, "Dừng lại", 0)
        self.assertTrue(thread_audio.should_interrupt(queues, 1))
        self.assertFalse(thread_audio.should_interrupt(queues, 0))

    def test_mock_pipeline_with_ocr_shutdown(self):
        self.assertEqual(run(load_config(mock=True), duration=.8, mock_button_after=.2), 0)

    def test_worker_exception_sets_fatal_and_stop(self):
        queues, stop = make_queues(load_config(mock=True)), threading.Event()
        def fail(*args):
            raise ValueError("Lỗi kiểm thử có chủ đích")
        with self.assertLogs(level="ERROR"):
            guarded_run("fake", SimpleNamespace(run_thread=fail), queues, stop)
        self.assertTrue(stop.is_set())
        events = list(queues["event_queue"].queue)
        self.assertTrue(any(item["kind"] == "fatal" for item in events))

    def test_stm32_fragmented_crc_duplicates_and_reset(self):
        queues, stop = make_queues(load_config()), threading.Event()
        packet = encode(BUTTON, 1, struct.pack("<IB", 1, 1))
        bad = bytearray(encode(BUTTON, 2, struct.pack("<IB", 2, 1)))
        bad[-1] ^= 0xff
        raw = packet*2+bad+encode(BOOT, 0)+encode(BUTTON, 1, struct.pack("<IB", 1, 2))
        fragments = [raw[i:i+7] for i in range(0, len(raw), 7)]
        acks = []
        def read(n):
            if fragments:
                return fragments.pop(0)
            stop.set()
            return b""
        def write(data):
            acks.extend(frame for frame in feed(new_decoder(), data) if frame[0] == BUTTON_ACK)
            return len(data)
        port = SimpleNamespace(read=read, write=write, close=lambda: None)
        fake_serial = SimpleNamespace(Serial=lambda *a, **kw: port, SerialException=OSError)
        with patch.dict(sys.modules, {"serial": fake_serial}):
            thread_stm32.run_thread(queues, stop)
        buttons = list(queues["stm32_queue"].queue)
        self.assertEqual([b["command"] for b in buttons], ["BTN_SHORT", "BTN_CURRENCY"])
        self.assertEqual(acks, [(BUTTON_ACK, 1, struct.pack("<I", 1))]*3)

    def test_full_button_queue_does_not_ack_unaccepted_event(self):
        queues, stop = make_queues(load_config()), threading.Event()
        for i in range(32):
            queues["stm32_queue"].put_nowait(i)
        data = [encode(BUTTON, 1, struct.pack("<IB", 1, 1))]
        acks = []
        def read(n):
            if data:
                return data.pop()
            stop.set()
            return b""
        def write(value):
            acks.extend(frame for frame in feed(new_decoder(), value) if frame[0] == BUTTON_ACK)
            return len(value)
        port = SimpleNamespace(read=read, write=write, close=lambda: None)
        with patch.dict(sys.modules, {"serial": SimpleNamespace(Serial=lambda *a, **kw: port, SerialException=OSError)}):
            thread_stm32.run_thread(queues, stop)
        self.assertEqual(acks, [])

    def test_expired_prompt_emits_cancelled(self):
        queues, stop = make_queues(load_config(mock=True)), threading.Event()
        ident = speak(queues, "Lời đã cũ", 2, ttl=-1)
        thread = threading.Thread(target=thread_audio.run_thread, args=(queues, stop))
        thread.start()
        deadline = time.monotonic()+2
        cancelled = False
        try:
            while time.monotonic() < deadline:
                item = queues["event_queue"].get(timeout=1)
                if item["kind"] == "audio_cancelled" and item["audio_id"] == ident:
                    cancelled = True
                    break
        finally:
            stop.set(); thread.join(2)
        self.assertTrue(cancelled)
        self.assertFalse(thread.is_alive())

    def test_urgent_audio_terminates_running_process(self):
        queues, stop = make_queues(load_config(mock=True)), threading.Event()
        processes = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        timer = threading.Timer(.15, lambda: speak(queues, "Dừng lại", 0))
        timer.start()
        try:
            with patch.object(thread_audio.subprocess, "Popen", side_effect=spawn):
                result = thread_audio.run_process([sys.executable, "-c", "import time; time.sleep(10)"], queues, stop, 2, 2)
            self.assertFalse(result)
            self.assertIsNotNone(processes[0].poll())
        finally:
            timer.cancel()

if __name__ == "__main__":
    unittest.main()
