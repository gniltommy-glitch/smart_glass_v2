"""Cầu nối WebSocket và REST API kết nối Kính thông minh (Smart Glass v2) với Mobile App.

Chuẩn giao thức: SecondSight protocol v1.
- REST và WebSocket cùng chạy trên cổng 8765 (hoặc cấu hình cfg['network']['port']).
- Xác thực bằng Bearer token trong header (cấu hình cfg['network']['token'] hoặc env SECONDSIGHT_TOKEN).
- Truyền nhận trạng thái dẫn đường, telemetry cảm biến, cảnh báo điểm mù / vật cản.
"""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from aiohttp import web, WSMsgType

from ..utils.channels import beat, event, latest_put


def read_cpu_temp():
    try:
        thermal_path = "/sys/class/thermal/thermal_zone0/temp"
        if os.path.exists(thermal_path):
            with open(thermal_path, "r") as f:
                return round(float(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass
    return None


def compare_heading(target_bearing, current_heading, tolerance=15.0):
    if target_bearing is None or current_heading is None:
        return {"heading_valid": False, "turn_confirmed": False, "heading_error": None}
    try:
        target = float(target_bearing)
        current = float(current_heading)
        error = ((target - current + 180) % 360) - 180
        confirmed = abs(error) <= tolerance
        return {
            "heading_valid": True,
            "turn_confirmed": confirmed,
            "heading_error": round(error, 1),
        }
    except (ValueError, TypeError):
        return {"heading_valid": False, "turn_confirmed": False, "heading_error": None}


class SmartGlassAppBridge:
    def __init__(self, queues, token):
        self.queues = queues
        self.token = token
        self.peers = set()
        self.nav_state = {"state": "idle", "revision": -1, "session_id": None}
        self.settings = {"warning_level": "normal", "volume": 0.8, "speech_rate": 0.5, "vibration": True}
        self.last_seq = -1
        self.latest_heading = None
        self.latest_pitch = 0.0
        self.latest_roll = 0.0
        self.stm32_calibrated = False
        self.yolo_fps = 0.0

    async def ws_handler(self, request):
        ws = web.WebSocketResponse(max_msg_size=262144, heartbeat=10)
        await ws.prepare(request)
        self.peers.add(ws)
        logging.info("App di động đã kết nối WebSocket (%d client)", len(self.peers))
        try:
            async for frame in ws:
                if frame.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(frame.data)
                    if not isinstance(msg, dict):
                        continue
                    mtype = msg.get("type")
                    if mtype == "ping":
                        await ws.send_json({"type": "pong"})
                    elif mtype == "navigation_instruction":
                        seq = msg.get("sequence", 0)
                        self.last_seq = seq
                        target = msg.get("target_bearing")
                        status = compare_heading(target, self.latest_heading)
                        await ws.send_json({
                            "type": "turn_status",
                            "sequence": seq,
                            **status,
                        })
                    elif mtype == "audio_yielded":
                        event(self.queues, "audio_yielded", hazard_id=msg.get("hazard_id"))
                except Exception as exc:
                    logging.warning("Lỗi xử lý frame WebSocket từ app: %s", exc)
        finally:
            self.peers.discard(ws)
            logging.info("App di động ngắt kết nối WebSocket")
        return ws

    async def broadcast_outbox(self):
        """Đọc tin nhắn từ app_outbox và gửi tới các app client đang kết nối."""
        outbox = self.queues.get("app_outbox")
        if outbox is None:
            return
        while True:
            try:
                item = outbox.get_nowait()
                outbox.task_done()
                if item and self.peers:
                    for peer in list(self.peers):
                        try:
                            await peer.send_json(item)
                        except Exception:
                            self.peers.discard(peer)
            except Exception:
                pass
            await asyncio.sleep(0.05)

    async def telemetry_broadcast(self):
        """Gửi device_status chu kỳ 1s về cho app."""
        cfg = self.queues["config"]
        while True:
            if self.peers:
                temp = read_cpu_temp()
                status_payload = {
                    "type": "device_status",
                    "demo": bool(cfg["runtime"].get("mock", False)),
                    "yolo_fps": round(self.yolo_fps, 1),
                    "cpu_temp": temp,
                    "battery_percent": None,
                    "camera_ok": True,
                    "lidar_ok": True,
                    "stm32_ok": self.latest_heading is not None or bool(cfg["runtime"].get("mock")),
                    "stm32_calibrated": self.stm32_calibrated or bool(cfg["runtime"].get("mock")),
                    "stm32_heading": self.latest_heading if self.latest_heading is not None else 90.0,
                    "stm32_pitch": self.latest_pitch,
                    "stm32_roll": self.latest_roll,
                }
                for peer in list(self.peers):
                    try:
                        await peer.send_json(status_payload)
                    except Exception:
                        self.peers.discard(peer)
            await asyncio.sleep(1.0)

    async def navigation_state(self, request):
        try:
            data = await request.json()
            assert isinstance(data, dict)
            assert data.get("protocol") == 1
            assert isinstance(data.get("session_id"), str)
            assert isinstance(data.get("revision"), int)
            assert data.get("state") in {"idle", "ready", "navigating", "paused", "arrived"}
        except (ValueError, AssertionError, KeyError):
            raise web.HTTPBadRequest(text="Sai định dạng snapshot trạng thái dẫn đường")

        cur = self.nav_state
        if data["session_id"] == cur.get("session_id") and data["revision"] < cur.get("revision", -1):
            raise web.HTTPConflict(text="Revision cũ hơn hiện tại")

        self.nav_state = data
        logging.info("Đã cập nhật trạng thái dẫn đường từ app: state=%s, revision=%d",
                     data["state"], data["revision"])
        return web.json_response({"ok": True, "revision": data["revision"]})

    async def configure_settings(self, request):
        try:
            j = await request.json()
            assert isinstance(j, dict)
            assert j.get("warning_level") in {"low", "normal", "detailed"}
            assert isinstance(j.get("volume"), (int, float)) and 0 <= j["volume"] <= 1
            assert isinstance(j.get("speech_rate"), (int, float)) and 0.2 <= j["speech_rate"] <= 0.8
        except (ValueError, KeyError, TypeError, AssertionError):
            raise web.HTTPBadRequest(text="Cài đặt không hợp lệ")

        self.settings.update(j)
        return web.json_response({"ok": True})

    async def get_status(self, _):
        return web.json_response({
            "demo": False,
            "state": self.nav_state.get("state", "idle"),
            "connected_apps": len(self.peers),
        })

    async def audio_test(self, _):
        event(self.queues, "audio_test")
        return web.json_response({"ok": True})

    async def demo_obstacle_avoid(self, request):
        """Demo Cấp độ 1: Vật cản nhỏ / né cục bộ vài bước."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        direction = body.get("avoid_direction", "left")
        dist = body.get("distance_m", 2.0)
        msg = body.get("message", f"Phát hiện vật cản nhỏ trên vỉa hè cách {dist} mét. Vui lòng bước sang {direction} để né.")
        from ..utils.channels import notify_app
        notify_app(self.queues, {
            "type": "obstacle_avoid",
            "avoid_direction": direction,
            "distance_m": dist,
            "message": msg,
        })
        return web.json_response({"ok": True, "level": 1, "type": "obstacle_avoid"})

    async def demo_path_blocked(self, request):
        """Demo Cấp độ 2: Bị chặn hoàn toàn lối đi -> app dựng lại lộ trình."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = body.get("reason", "blocked_path")
        dist = body.get("distance_m", 3.0)
        msg = body.get("message", "Lối đi bị chặn hoàn toàn do rào chắn/công trình. Đang yêu cầu tìm đường mới.")
        from ..utils.channels import notify_app
        notify_app(self.queues, {
            "type": "path_blocked",
            "reason": reason,
            "distance_m": dist,
            "avoid_direction": body.get("avoid_direction", "left"),
            "message": msg,
        })
        return web.json_response({"ok": True, "level": 2, "type": "path_blocked"})


def create_app(bridge, token):
    @web.middleware
    async def auth_middleware(request, handler):
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {token}"):
            raise web.HTTPUnauthorized(text="Yêu cầu Bearer token ghép đôi của Kính")
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware], client_max_size=2 * 1024 * 1024)
    app.router.add_get("/ws", bridge.ws_handler)
    app.router.add_post("/navigation/state", bridge.navigation_state)
    app.router.add_post("/settings", bridge.configure_settings)
    app.router.add_get("/device/status", bridge.get_status)
    app.router.add_post("/audio/test", bridge.audio_test)
    app.router.add_post("/demo/obstacle_avoid", bridge.demo_obstacle_avoid)
    app.router.add_post("/demo/path_blocked", bridge.demo_path_blocked)
    return app


def run_thread(queues, stop_event):
    cfg = queues["config"]
    net_cfg = cfg.get("network", {})
    if not net_cfg.get("enabled", True):
        event(queues, "ready", worker="app_link")
        while not stop_event.is_set():
            beat(queues, "app_link")
            stop_event.wait(1.0)
        return

    token = os.environ.get("SECONDSIGHT_TOKEN", net_cfg.get("token", "secondsight-default-pairing-secret-key-32ch"))
    host = net_cfg.get("host", "0.0.0.0")
    port = net_cfg.get("port", 8765)

    bridge = SmartGlassAppBridge(queues, token)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = create_app(bridge, token)
    runner = web.AppRunner(app, access_log=None)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())

    logging.info("Smart Glass App Link đã mở tại http://%s:%d/ws", host, port)
    event(queues, "ready", worker="app_link")

    broadcast_task = loop.create_task(bridge.broadcast_outbox())
    telemetry_task = loop.create_task(bridge.telemetry_broadcast())

    try:
        while not stop_event.is_set():
            beat(queues, "app_link")
            # Cập nhật thông tin cảm biến từ event_queue hoặc status_queue
            status_q = queues.get("audio_status_queue")
            if status_q:
                try:
                    s = status_q.get_nowait()
                    status_q.task_done()
                    if s and "heading" in s:
                        bridge.latest_heading = s.get("heading")
                        bridge.latest_pitch = s.get("pitch", 0.0)
                        bridge.latest_roll = s.get("roll", 0.0)
                        bridge.stm32_calibrated = s.get("calibrated", False)
                except Exception:
                    pass
            loop.run_until_complete(asyncio.sleep(0.1))
    finally:
        broadcast_task.cancel()
        telemetry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(broadcast_task)
            loop.run_until_complete(telemetry_task)
        for ws in list(bridge.peers):
            loop.run_until_complete(ws.close())
        loop.run_until_complete(runner.cleanup())
        loop.close()
