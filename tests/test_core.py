"""Kiểm thử thuật toán và giao thức với dữ liệu cố định, không cần phần cứng."""
import ast
import binascii
from pathlib import Path
import queue
import struct
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np

from smart_glass_v2.config import ROOT, load_config
from smart_glass_v2.main_coordinator import new_state, handle_audio_event, handle_button, process_scene, process_tof
from smart_glass_v2.modules.thread_tof import read_reply
from smart_glass_v2.modules.thread_ocr import read_text, read_currency
from smart_glass_v2.utils.channels import make_queues, latest_put, latest_get, speak
from smart_glass_v2.utils.crossing import new_observation, update_observation
from smart_glass_v2.utils.nav_algorithm import obstacle_summary, extract_corridor
from smart_glass_v2.utils.spatial_matcher import normalize_matrix, matching_cells, match_distance, sector
from smart_glass_v2.utils.text_postprocess import clean_text, speech_chunks
from smart_glass_v2.utils.uart_protocol import encode, feed, new_decoder, START, PCM, STOP, BUTTON, ACK

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(mock=True)
        self.queues = make_queues(self.cfg)

    def test_python_311_syntax(self):
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))

    def test_uart_crc_and_corruption(self):
        self.assertEqual(binascii.crc_hqx(b"123456789", 0xffff), 0x29b1)
        for cmd in (START, PCM, STOP, BUTTON, ACK):
            packet = encode(cmd, 65535, b"123")
            self.assertEqual(feed(new_decoder(), packet), [(cmd, 65535, b"123")])
            bad = bytearray(packet)
            bad[-1] ^= 0xff
            self.assertEqual(feed(new_decoder(), bad), [])
        with self.assertRaises(ValueError):
            encode(PCM, 1, b"0"*1025)
        decoder = new_decoder()
        self.assertEqual(feed(decoder, b"noise"+encode(PCM, 8, b"test")[:6], now=1), [])
        self.assertEqual(feed(decoder, encode(PCM, 8, b"test")[6:], now=1.01), [(PCM, 8, b"test")])
        feed(decoder, b"\xa5\x5a", now=2)
        self.assertEqual(feed(decoder, encode(STOP, 9), now=3), [(STOP, 9, b"")])

    def test_tof_fifo_128_bytes_in_chunks(self):
        payload = struct.pack("<64H", *range(1000, 1064))
        fifo = bytearray(b"\xff\x53\x02\x80\x00"+payload)
        lengths = []
        def read(length):
            lengths.append(length)
            result = bytes(fifo[:length])
            del fifo[:length]
            return result
        self.assertEqual(read_reply(read, 2, threading.Event()), payload)
        self.assertEqual(lengths, [1, 1, 1, 2, 32, 32, 32, 32])
        self.assertEqual(len(fifo), 0)

    def test_tof_error_and_timeout(self):
        for raw in (b"\x63\x02\x00\x00", b"\x53\x01\x00\x00", b"\x53\x02\x01\x01"):
            fifo = bytearray(raw)
            def read(length):
                out = bytes(fifo[:length]); del fifo[:length]
                return out
            with self.assertRaises(ValueError):
                read_reply(read, 2, threading.Event())
        with self.assertRaises(TimeoutError):
            read_reply(lambda n: b"\xff"*n, 2, threading.Event(), timeout=0.02)

    def test_invalid_tof_and_orientation(self):
        raw = np.full((8, 8), 1000.0)
        raw[0, :5] = [0, 19, 4000, 65535, np.nan]
        result = normalize_matrix(raw, self.cfg["tof"])
        self.assertTrue(np.isnan(result[0, :5]).all())
        self.assertEqual(result[7, 7], 1)
        self.cfg["tof"].update(rotate_quarters=1, flip_x=True)
        np.testing.assert_equal(normalize_matrix(raw, self.cfg["tof"]), np.fliplr(np.rot90(result)))

    def test_spatial_center_outside_and_nine_sectors(self):
        cells = matching_cells([300, 160, 340, 200], [640, 360], self.cfg["camera"], self.cfg["tof"])
        self.assertEqual(set(cells), {(3, 3), (3, 4), (4, 3), (4, 4)})
        self.assertEqual(matching_cells([0, 160, 10, 200], [640, 360], self.cfg["camera"], self.cfg["tof"]), [])
        sectors = {sector([x, y, x+1, y+1], (640, 360)) for x in (50, 320, 600) for y in (50, 180, 320)}
        self.assertEqual(len(sectors), 9)

    def test_distance_needs_calibration_freshness_and_valid_cells(self):
        cfg = self.cfg["tof"]
        sample = {"time": 10, "matrix": np.full((8, 8), 2.0)}
        args = ([300, 160, 340, 200], (640, 360), self.cfg["camera"], cfg, sample)
        self.assertIsNone(match_distance(*args, 10, 10.1))
        cfg["calibrated"] = True
        self.assertEqual(match_distance(*args, 10, 10.1), 2)
        self.assertIsNone(match_distance(*args, 10, 11))
        self.assertIsNone(match_distance(*args, 9, 10.1))
        sample["matrix"][:] = np.nan
        self.assertIsNone(match_distance(*args, 10, 10.1))

    def test_corridor_does_not_hide_near_or_unknown_cells(self):
        matrix = np.full((8, 8), np.nan)
        self.assertIsNone(extract_corridor(matrix, self.cfg["navigation"])["direction"])
        matrix[:] = 3
        matrix[4, :] = 0.5
        self.assertIsNone(extract_corridor(matrix, self.cfg["navigation"])["direction"])
        matrix[:, :3] = 2
        self.assertEqual(extract_corridor(matrix, self.cfg["navigation"])["direction"], "left")
        matrix[:, :3] = 0.5
        matrix[:, 5:] = 2
        self.assertEqual(extract_corridor(matrix, self.cfg["navigation"])["direction"], "right")

    def test_more_than_four_near_cells_or_dense_objects(self):
        matrix = np.full((8, 8), 2.0)
        matrix[0, :4] = .6
        self.assertFalse(obstacle_summary(matrix, 0, self.cfg["navigation"])["triggered"])
        matrix[0, 4] = .6
        self.assertTrue(obstacle_summary(matrix, 0, self.cfg["navigation"])["triggered"])
        matrix[:] = 2
        self.assertTrue(obstacle_summary(matrix, 5, self.cfg["navigation"])["triggered"])

    def vehicle_scene(self, ident, dx=0, scale=1, camera_motion=0, vehicles=True):
        half = .1*scale
        return {"id": ident, "time": ident*.2, "camera_motion": camera_motion,
                "vehicles": [{"label": "car", "bbox": [.5+dx-half, .5-half, .5+dx+half, .5+half]}] if vehicles else []}

    def test_crossing_ten_unique_frames(self):
        state = new_observation()
        for i in range(9):
            self.assertIsNone(update_observation(state, self.vehicle_scene(i), self.cfg["crossing"]))
        self.assertIsNone(update_observation(state, self.vehicle_scene(8), self.cfg["crossing"]))
        self.assertEqual(state["frames"], 9)
        self.assertEqual(update_observation(state, self.vehicle_scene(9), self.cfg["crossing"]), "apparently_still")

    def test_crossing_motion_approach_empty_shake_and_gap(self):
        cases = [({"dx": lambda i: i*.01}, "moving"),
                 ({"scale": lambda i: 1+i*.01}, "moving"),
                 ({"vehicles": lambda i: False}, "uncertain"),
                 ({"camera_motion": lambda i: None}, "uncertain"),
                 ({"camera_motion": lambda i: 10}, "uncertain")]
        for changes, expected in cases:
            state = new_observation()
            for i in range(10):
                result = update_observation(state, self.vehicle_scene(i, **{k: v(i) for k, v in changes.items()}), self.cfg["crossing"])
            self.assertEqual(result, expected)
        state = new_observation()
        for i in range(10):
            scene = self.vehicle_scene(i)
            scene["time"] = i*2
            result = update_observation(state, scene, self.cfg["crossing"])
        self.assertEqual(result, "uncertain")

    def test_confirm_window_starts_after_prompt_done(self):
        state = new_state()
        state.update(mode="CROSS_PROMPT", audio_id=123)
        handle_button(state, self.queues, {"command": "BTN_SHORT", "time": 10}, 10)
        self.assertEqual(state["mode"], "CROSS_PROMPT")
        handle_audio_event(state, {"kind": "audio_done", "audio_id": 123, "time": 20}, self.cfg)
        self.assertEqual(state["deadline"], 25)
        handle_button(state, self.queues, {"command": "BTN_SHORT", "time": 24}, 24)
        self.assertEqual(state["mode"], "CROSS_OBSERVE")
        self.assertTrue(self.queues["camera_requests"].empty())

    def test_button_context_ocr_currency_stale_and_busy(self):
        for command, mode in [("BTN_SHORT", "OCR_BUSY"), ("BTN_CURRENCY", "CURRENCY_BUSY")]:
            state = new_state()
            handle_button(state, self.queues, {"command": command, "time": 1}, 10)
            self.assertEqual(state["mode"], "IDLE")
            handle_button(state, self.queues, {"command": command, "time": 10}, 10)
            self.assertEqual(state["mode"], mode)
            handle_button(state, self.queues, {"command": command, "time": 10}, 10)
            self.assertEqual(state["request_id"], 1)

    def test_emergency_cancels_crossing_and_blocks_new_prompt(self):
        state = new_state()
        state.update(mode="CROSS_WAIT", capabilities={"road": True, "crossing": True})
        now = time.monotonic()
        matrix = np.full((8, 8), 2.0)
        matrix[3, 3] = .4
        tof = {"id": 1, "time": now, "matrix": matrix}
        process_tof(state, self.queues, tof, now)
        process_scene(state, self.queues, {"id": 1, "time": now}, tof, now)
        self.assertEqual(state["mode"], "IDLE")
        item = self.queues["audio_queue"].get_nowait()
        self.assertEqual(item[0], 0)
        self.assertIn("dừng lại", item[2]["text"])

    def test_dense_objects_get_corridor(self):
        self.cfg["tof"]["calibrated"] = True
        now = time.monotonic()
        matrix = np.ones((8, 8))
        matrix[:, :3] = 2
        scene = {"id": 1, "time": now, "zebra": False, "detections": [{"label": "person"}]*5}
        process_scene(new_state(), self.queues, scene, {"matrix": matrix, "time": now}, now)
        self.assertIn("bên trái", self.queues["audio_queue"].get_nowait()[2]["text"])

    def test_queue_bounds_and_audio_priorities(self):
        channel = queue.Queue(maxsize=1)
        latest_put(channel, 1); latest_put(channel, 2)
        self.assertEqual(latest_get(channel), 2)
        self.assertEqual(channel.unfinished_tasks, 0)
        for i in range(32):
            speak(self.queues, f"Văn bản {i}")
        speak(self.queues, "Cảnh báo", 0)
        self.assertEqual(self.queues["audio_queue"].qsize(), 1)
        self.assertEqual(self.queues["audio_queue"].get_nowait()[0], 0)
        self.assertTrue(self.queues["urgent_audio"].is_set())

    def test_vietnamese_cleanup_keeps_numbers(self):
        text = clean_text("  Tôi  có 20.000 đồng ,  hôm nay .\nDòng hai\nDòng hai", {}, 200)
        self.assertIn("Tôi có 20.000 đồng, hôm nay.", text)
        self.assertEqual(text.count("Dòng hai"), 1)
        self.assertTrue(all(len(chunk) <= 220 for chunk in speech_chunks("xin chào "*100)))

    def test_ocr_crops_and_filters_low_confidence(self):
        def detector(frame, **kwargs):
            return np.array([[[1, 1], [30, 1], [30, 10], [1, 10]],
                             [[1, 20], [30, 20], [30, 30], [1, 30]]]), None
        calls = []
        def predict(image, return_prob):
            calls.append(image.size)
            return ("20.000 đồng", .95) if len(calls) == 1 else ("chữ đoán sai", .2)
        from types import SimpleNamespace
        text = read_text(np.zeros((40, 50, 3), np.uint8), detector,
                         SimpleNamespace(predict=predict), self.cfg["ocr"], threading.Event())
        self.assertEqual(text, "20.000 đồng")
        self.assertEqual(len(calls), 2)

if __name__ == "__main__":
    unittest.main()
