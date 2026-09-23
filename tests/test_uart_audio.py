"""Kiểm thử truyền PCM/nút đồng thời, retry không lặp mẫu và ngắt lời đang phát."""
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import unittest
import uuid
import wave
from types import SimpleNamespace
from unittest.mock import patch

from smart_glass_v2.config import ROOT, load_config
from smart_glass_v2.modules import thread_stm32
from smart_glass_v2.utils.channels import make_queues
from smart_glass_v2.utils.uart_audio import play_wav
from smart_glass_v2.utils.uart_protocol import (encode, feed, new_decoder, parse_status,
                                               START, PCM, STOP, ACK, BUTTON, BUTTON_ACK, DONE, BOOT)

class FakeBoard:
    """Thiết bị giả có ring đầy, ACK mất và DONE trễ; không mô phỏng mạch analog."""
    def __init__(self, interrupt=None):
        self.incoming = bytearray()
        self.decoder = new_decoder()
        self.received = bytearray()
        self.commands = []
        self.last = None
        self.busy_once = True
        self.drop_once = True
        self.play_id = self.samples = 0
        self.done_at = None
        self.done_released = False
        self.button_acks = 0
        self.interrupt = interrupt

    def write(self, data):
        for kind, seq, payload in feed(self.decoder, data):
            self.commands.append((kind, seq, payload))
            if kind == BUTTON_ACK:
                self.button_acks += 1
                continue
            signature = (kind, seq, payload)
            if signature == self.last:
                self.incoming.extend(encode(ACK, seq, b"\x00"))
                continue
            if kind == PCM and self.busy_once:
                self.busy_once = False
                self.incoming.extend(encode(ACK, seq, b"\x01"))
                self.incoming.extend(encode(BUTTON, 7, struct.pack("<IB", 7, 1)))
                continue
            self.last = signature
            if kind == START:
                self.play_id, self.samples = struct.unpack("<II", payload)
                self.received.clear()
            elif kind == PCM:
                self.received.extend(payload)
                if self.interrupt is not None:
                    self.interrupt.set()
                if len(self.received) == self.samples*2:
                    # DONE từ lần phát khác không được kết thúc lần hiện tại.
                    wrong_id = (self.play_id % 0xffffffff)+1
                    self.incoming.extend(encode(DONE, 0, struct.pack("<II", wrong_id, self.samples)))
                    self.done_at = time.monotonic()+.05
                if self.drop_once:
                    self.drop_once = False
                    continue
            self.incoming.extend(encode(ACK, seq, b"\x00"))
        return len(data)

    def read(self, size):
        time.sleep(.001)
        if self.done_at is not None and time.monotonic() >= self.done_at:
            self.done_released = True
            self.done_at = None
            self.incoming.extend(encode(DONE, 0, struct.pack("<II", self.play_id, self.samples)))
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def close(self):
        pass

class UARTAudioTests(unittest.TestCase):
    def setUp(self):
        self.queues = make_queues(load_config())
        self.stop = threading.Event()
        folder = ROOT/".cache"
        folder.mkdir(exist_ok=True)
        self.path = folder/("uart_test_"+uuid.uuid4().hex+".wav")
        self.pcm = struct.pack("<1600h", *range(1600))
        with wave.open(str(self.path), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(self.pcm)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def start_board(self, board):
        fake_serial = SimpleNamespace(Serial=lambda *a, **kw: board, SerialException=OSError)
        self.patcher = patch.dict(sys.modules, {"serial": fake_serial})
        self.patcher.start()
        self.thread = threading.Thread(target=thread_stm32.run_thread, args=(self.queues, self.stop))
        self.thread.start()

    def close_board(self):
        self.stop.set()
        self.thread.join(2)
        self.patcher.stop()
        self.assertFalse(self.thread.is_alive())

    def test_pcm_retries_preserve_samples_buttons_and_wait_for_matching_done(self):
        board = FakeBoard()
        self.start_board(board)
        try:
            self.assertTrue(play_wav(self.queues, self.path, self.stop))
            self.assertEqual(board.received, self.pcm)
            self.assertTrue(board.done_released)
            self.assertEqual(self.queues["stm32_queue"].get_nowait()["command"], "BTN_SHORT")
            self.assertEqual(board.button_acks, 1)
            pcm_commands = [c for c in board.commands if c[0] == PCM]
            self.assertGreater(len(pcm_commands), 4)
            self.assertEqual(pcm_commands[0][1], pcm_commands[1][1])
        finally:
            self.close_board()

    def test_urgent_warning_sends_stop_before_returning(self):
        interrupted = threading.Event()
        board = FakeBoard(interrupted)
        self.start_board(board)
        try:
            self.assertFalse(play_wav(self.queues, self.path, self.stop, interrupted.is_set))
            self.assertLess(len(board.received), len(self.pcm))
            self.assertEqual(board.commands[-1][0], STOP)
        finally:
            self.close_board()

    def test_reset_and_ack_in_same_read_is_failure(self):
        state = thread_stm32.new_link_state()
        incoming = []
        def write(data):
            _, seq, _ = feed(new_decoder(), data)[0]
            incoming.append(encode(BOOT, 0)+encode(ACK, seq, b"\x00"))
            return len(data)
        port = SimpleNamespace(write=write, read=lambda n: incoming.pop(0))
        job = {"kind": PCM, "payload": b"\x00\x00", "deadline": time.monotonic()+1,
               "cancel": threading.Event()}
        with self.assertRaisesRegex(OSError, "reset"):
            thread_stm32.execute(self.queues, port, state, job, self.stop)

    def test_wrong_wav_format_rejected_before_uart(self):
        with wave.open(str(self.path), "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(self.pcm)
        with self.assertRaises(ValueError):
            play_wav(self.queues, self.path, self.stop)
        self.assertTrue(self.queues["uart_tx_queue"].empty())

    def test_status_protocol_version_and_completion_fields(self):
        data = struct.pack("<4B9I", 2, 1, 0, 0, 1000, 0, 0, 0, 0, 1600, 99, 7, 14)
        status = parse_status(data)
        self.assertEqual(status["play_id"], 99)
        self.assertEqual(status["samples_played"], 1600)
        with self.assertRaises(ValueError):
            parse_status(bytes([1])+data[1:])
        with self.assertRaises(ValueError):
            parse_status(b"\x00"*48)

if __name__ == "__main__":
    unittest.main()
