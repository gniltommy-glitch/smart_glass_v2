"""Kiểm thử tính năng mở rộng Caregiver trên Smart Glass v2 (ảnh tĩnh JPEG & đọc lời nhắn)."""
import base64
import unittest
import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from smart_glass_v2.config import load_config
from smart_glass_v2.modules.thread_app_link import SmartGlassAppBridge, create_app
from smart_glass_v2.utils.channels import latest_put, make_queues

TOKEN = "test-token-at-least-24-chars-long-123456"


class CaregiverBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = load_config(mock=True)
        self.queues = make_queues(cfg)
        self.bridge = SmartGlassAppBridge(self.queues, TOKEN)
        self.app = create_app(self.bridge, TOKEN)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_snapshot_endpoint_returns_valid_jpeg_base64(self):
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        frame[:, :, 1] = 128
        latest_put(self.queues["snapshot_frame_queue"], {"frame": frame, "time": 1.0, "id": 7})

        resp = await self.client.post(
            "/caregiver/snapshot",
            headers=self.headers,
            json={"question": "Biển số xe buýt phía trước là số mấy?"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["type"], "assist_request")
        self.assertEqual(data["width"], 320)
        self.assertEqual(data["height"], 180)
        raw_jpeg = base64.b64decode(data["image_base64"])
        # Dấu hiệu SOI của chuẩn JPEG: 0xFF 0xD8
        self.assertEqual(raw_jpeg[:2], b"\xff\xd8")

    async def test_caregiver_speak_queues_priority_audio_for_piper(self):
        resp = await self.client.post(
            "/caregiver/speak",
            headers=self.headers,
            json={
                "request_id": "req-42",
                "sender_name": "Mẹ",
                "reply_text": "Đường phía trước thoáng, đi thẳng năm mét.",
            },
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["audio_queued"])

        priority, _, item = self.queues["audio_queue"].get_nowait()
        self.assertEqual(priority, 2)
        self.assertIn("Mẹ nhắn: Đường phía trước thoáng", item["text"])

    async def test_websocket_caregiver_reply_enqueues_speech(self):
        async with self.client.ws_connect("/ws", headers=self.headers) as ws:
            await ws.send_json({
                "type": "caregiver_reply",
                "request_id": "ws-01",
                "sender_name": "Ba",
                "reply_text": "Chờ đèn xanh rồi mới sang đường.",
            })
            await ws.send_json({"type": "ping"})
            pong = await ws.receive_json()
            self.assertEqual(pong["type"], "pong")

        _, _, item = self.queues["audio_queue"].get_nowait()
        self.assertIn("Ba nhắn: Chờ đèn xanh", item["text"])


if __name__ == "__main__":
    unittest.main()
