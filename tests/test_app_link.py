"""Kiểm tra cầu nối WebSocket và REST API giữa Smart Glass v2 và Mobile App."""
import asyncio
import json
import unittest
from aiohttp.test_utils import TestClient, TestServer
from smart_glass_v2.config import load_config
from smart_glass_v2.modules.thread_app_link import SmartGlassAppBridge, create_app, compare_heading
from smart_glass_v2.utils.channels import make_queues, notify_app

TOKEN = "test-token-at-least-24-chars-long-123456"

class AppLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = load_config(mock=True)
        self.queues = make_queues(cfg)
        self.bridge = SmartGlassAppBridge(self.queues, TOKEN)
        self.bridge.latest_heading = 90.0
        self.bridge.stm32_calibrated = True
        self.app = create_app(self.bridge, TOKEN)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_auth_required(self):
        resp = await self.client.get("/device/status")
        self.assertEqual(resp.status, 401)

        resp = await self.client.get("/device/status", headers=self.headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["state"], "idle")

    async def test_navigation_state_snapshot(self):
        snapshot = {
            "protocol": 1,
            "session_id": "test-session-1",
            "revision": 2,
            "state": "navigating",
            "route": {"id": "route-1", "points": [[106.66, 10.76]]},
        }
        resp = await self.client.post("/navigation/state", json=snapshot, headers=self.headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["revision"], 2)
        self.assertEqual(self.bridge.nav_state["state"], "navigating")

        # Gửi revision cũ hơn -> 409 Conflict
        old_snapshot = dict(snapshot, revision=1)
        resp2 = await self.client.post("/navigation/state", json=old_snapshot, headers=self.headers)
        self.assertEqual(resp2.status, 409)

    async def test_settings_update(self):
        settings = {
            "warning_level": "normal",
            "volume": 0.7,
            "speech_rate": 0.5,
            "vibration": True,
        }
        resp = await self.client.post("/settings", json=settings, headers=self.headers)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.bridge.settings["volume"], 0.7)

    async def test_ws_ping_pong_and_instruction(self):
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        await ws.send_json({"type": "ping"})
        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        self.assertEqual(msg["type"], "pong")

        # Instruction with target_bearing=92 vs current heading 90 -> confirmed!
        await ws.send_json({
            "type": "navigation_instruction",
            "sequence": 5,
            "target_bearing": 92.0,
            "action": "turn_right",
        })
        resp = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        self.assertEqual(resp["type"], "turn_status")
        self.assertEqual(resp["sequence"], 5)
        self.assertTrue(resp["heading_valid"])
        self.assertTrue(resp["turn_confirmed"])
        await ws.close()

    async def test_ws_broadcasts_path_blocked_from_outbox(self):
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        task = asyncio.create_task(self.bridge.broadcast_outbox())

        # Gửi sự kiện điểm mù / đường bị chặn từ coordinator
        notify_app(self.queues, {
            "type": "path_blocked",
            "reason": "blind_spot",
            "avoid_direction": "left",
            "distance_m": 3.0,
            "message": "Phát hiện điểm mù phía trước",
        })

        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        self.assertEqual(msg["type"], "path_blocked")
        self.assertEqual(msg["reason"], "blind_spot")
        self.assertEqual(msg["avoid_direction"], "left")

        task.cancel()
        await ws.close()

    def test_compare_heading_tolerance(self):
        res1 = compare_heading(90, 85, tolerance=15)
        self.assertTrue(res1["turn_confirmed"])
        self.assertEqual(res1["heading_error"], 5.0)

        res2 = compare_heading(90, 120, tolerance=15)
        self.assertFalse(res2["turn_confirmed"])
        self.assertEqual(res2["heading_error"], -30.0)

        res3 = compare_heading(5, 355, tolerance=15)
        self.assertTrue(res3["turn_confirmed"])
        self.assertEqual(res3["heading_error"], 10.0)


if __name__ == "__main__":
    unittest.main()
