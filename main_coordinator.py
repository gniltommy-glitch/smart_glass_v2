"""Điều phối các luồng bằng queue và máy trạng thái, không sở hữu thiết bị phần cứng."""
import argparse
import importlib
import importlib.util
import logging
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from smart_glass_v2.config import ROOT, load_config
from smart_glass_v2.utils.channels import make_queues, guarded_run, latest_get, speak
from smart_glass_v2.utils.crossing import new_observation, update_observation
from smart_glass_v2.utils.nav_algorithm import obstacle_summary
from smart_glass_v2.utils.spatial_matcher import match_distance, sector

WORKERS = ("camera", "tof", "vision", "ocr", "stm32", "audio")

def new_state():
    return {"mode": "IDLE", "deadline": 0.0, "audio_id": None, "request_id": 0,
            "last_scene": None, "last_tof": None, "cooldowns": {}, "cross_next": 0.0,
            "capabilities": {"road": False, "crossing": False}, "observation": None,
            "request_created": 0.0}

def announce(state, queues, key, text, priority=5, interval=5.0, ttl=8.0):
    now = time.monotonic()
    if now-state["cooldowns"].get(key, -999) < interval:
        return None
    ident = speak(queues, text, priority, key, ttl)
    if ident is not None:
        state["cooldowns"][key] = now
    return ident

def handle_audio_event(state, item, cfg):
    if state["mode"] == "CROSS_PROMPT" and item.get("audio_id") == state["audio_id"]:
        if item["kind"] == "audio_done":
            state.update(mode="CROSS_WAIT", deadline=item["time"]+cfg["crossing"]["confirm_s"])
        elif item["kind"] == "audio_cancelled":
            state["mode"] = "IDLE"

def handle_button(state, queues, item, now):
    cfg, command = queues["config"], item["command"]
    if now-item["time"] > 2.0:
        return
    if command == "BTN_SHORT":
        if state["mode"] == "CROSS_PROMPT":
            return
        command = "BTN_CONFIRM" if state["mode"] == "CROSS_WAIT" else "BTN_OCR"
    if command == "BTN_CONFIRM":
        if state["mode"] == "CROSS_WAIT" and now <= state["deadline"]:
            state.update(mode="CROSS_OBSERVE", observation=new_observation(),
                         deadline=now+cfg["crossing"]["observation_timeout_s"])
            speak(queues, "Đang quan sát chuyển động của xe. Hãy đứng yên và chờ.", 2, ttl=8)
        return
    if command not in ("BTN_OCR", "BTN_CURRENCY"):
        return
    if state["mode"] != "IDLE":
        announce(state, queues, "busy", "Đang xử lý yêu cầu trước, vui lòng chờ.", 3)
        return
    kind = "ocr" if command == "BTN_OCR" else "currency"
    ident = state["request_id"]+1
    try:
        queues["camera_requests"].put_nowait({"kind": kind, "request_id": ident, "created": now})
    except queue.Full:
        announce(state, queues, "busy", "Camera đang bận, vui lòng thử lại.", 3)
        return
    state.update(mode="OCR_BUSY" if kind == "ocr" else "CURRENCY_BUSY", request_id=ident,
                 request_created=now, deadline=now+cfg["runtime"]["request_timeout_s"])
    speak(queues, "Đang chụp văn bản." if kind == "ocr" else "Đang quan sát tờ tiền.", 2, ttl=8)

def process_scene(state, queues, scene, tof, now):
    cfg = queues["config"]
    if scene is None or now-scene["time"] > cfg["vision"]["result_max_age_s"] or scene["id"] == state["last_scene"]:
        return
    state["last_scene"] = scene["id"]
    fresh_tof = tof is not None and now-tof["time"] <= cfg["tof"]["max_age_s"]
    if fresh_tof and obstacle_summary(tof["matrix"], 0, cfg["navigation"])["emergency"]:
        return
    if state["mode"] == "CROSS_OBSERVE":
        result = update_observation(state["observation"], scene, cfg["crossing"])
        if result:
            text = {"moving": "Còn xe đang di chuyển, vui lòng chờ.",
                    "uncertain": "Chưa đủ dữ liệu để đánh giá chuyển động của xe. Vui lòng chờ.",
                    "apparently_still": "Trong các khung hình vừa quan sát, chưa thấy xe chuyển động rõ. Chưa thể xác nhận an toàn sang đường."}[result]
            speak(queues, text, 1, ttl=10)
            state.update(mode="IDLE", cross_next=now+cfg["crossing"]["cooldown_s"])
    if state["capabilities"]["road"] and scene["road_coverage"] >= cfg["navigation"]["road_coverage"] and scene["sidewalk_coverage"] < cfg["navigation"]["sidewalk_coverage"]:
        announce(state, queues, "road", "Bạn có thể đang đi xuống lòng đường. Hãy dừng lại và xác định lại vị trí vỉa hè.", 0, interval=4)
        if state["mode"].startswith("CROSS_"):
            state.update(mode="IDLE", cross_next=now+cfg["crossing"]["cooldown_s"])
        return
    if state["mode"] == "IDLE" and scene["zebra"] and state["capabilities"]["crossing"] and now >= state["cross_next"]:
        ident = speak(queues, "Phía trước có vạch sang đường. Bạn có muốn sang đường không? Bấm nút để xác nhận.", 2, ttl=10)
        if ident is not None:
            state.update(mode="CROSS_PROMPT", audio_id=ident, deadline=now+45,
                         cross_next=now+cfg["crossing"]["cooldown_s"])
        return
    ignored = set(cfg["labels"]["road"]+cfg["labels"]["sidewalk"]+cfg["labels"]["zebra"])
    objects = [d for d in scene["detections"] if d["label"] not in ignored]
    if len(objects) >= cfg["navigation"]["crowd_objects"]:
        summary = obstacle_summary(tof["matrix"], len(objects), cfg["navigation"]) if fresh_tof else {"direction": None}
        announce_corridor(state, queues, summary)
        return
    if state["mode"] != "IDLE" or not 1 <= len(objects) <= 3:
        return
    messages = []
    for obj in objects:
        name = cfg["labels"]["vi"].get(obj["label"], "vật thể")
        position = sector(obj["bbox"], scene["size"])
        distance = match_distance(obj["bbox"], scene["size"], cfg["camera"], cfg["tof"], tof, scene["time"], now)
        if distance is None:
            messages.append(f"Có {name} ở phía {position}, chưa xác định được khoảng cách.")
        else:
            number = f"{distance:.1f}".replace(".", " phẩy ")
            messages.append(f"Có {name} ở phía {position}, cách khoảng {number} mét.")
    announce(state, queues, "objects", " ".join(messages), 5, interval=cfg["runtime"]["repeat_s"])

def announce_corridor(state, queues, summary):
    text = "Có nhiều chướng ngại vật phía trước."
    if queues["config"]["tof"]["calibrated"] and summary["direction"]:
        direction = {"left": "bên trái", "right": "bên phải", "straight": "chính giữa"}[summary["direction"]]
        text += f" Khoảng trống quan sát được lớn hơn ở phía {direction}. Hãy kiểm tra trước khi di chuyển."
    else:
        text += " Chưa xác định được lối đi phù hợp."
    announce(state, queues, "crowd", text, 1, interval=5)

def process_tof(state, queues, tof, now):
    cfg = queues["config"]
    if tof is None or now-tof["time"] > cfg["tof"]["max_age_s"]:
        return
    if state["last_tof"] == tof["id"]:
        return
    state["last_tof"] = tof["id"]
    summary = obstacle_summary(tof["matrix"], 0, cfg["navigation"])
    if summary["emergency"]:
        announce(state, queues, "near", "Cảnh báo! Vật cản quá gần phía trước. Hãy dừng lại.", 0, interval=3)
        if state["mode"].startswith("CROSS_"):
            state.update(mode="IDLE", cross_next=now+cfg["crossing"]["cooldown_s"])
    elif summary["triggered"]:
        announce_corridor(state, queues, summary)

def check_files(cfg, hardware=True):
    errors = []
    if not cfg["runtime"]["mock"]:
        for section, key in [("vision", "model"), ("audio", "model"), ("ocr", "detector"), ("ocr", "config"), ("ocr", "weights")]:
            if not Path(cfg[section][key]).exists():
                errors.append(f"Thiếu {section}.{key}: {cfg[section][key]}")
        if not Path(cfg["audio"]["model"]+".json").is_file():
            errors.append("Thiếu file .onnx.json đi kèm giọng Piper")
        for module in ("cv2", "numpy", "picamera2", "serial", "smbus2", "ultralytics", "piper", "vietocr", "rapidocr_onnxruntime"):
            if importlib.util.find_spec(module) is None:
                errors.append(f"Thiếu thư viện Python: {module}")
        if hardware:
            import shutil
            for device in [f"/dev/i2c-{cfg['tof']['bus']}", cfg["stm32"]["port"]]:
                if not os.access(device, os.R_OK | os.W_OK):
                    errors.append(f"Không có quyền đọc/ghi thiết bị: {device}")
            if shutil.which("ffmpeg") is None:
                errors.append("Chưa cài bộ chuyển đổi âm thanh: sudo apt install ffmpeg")
    for error in errors:
        logging.error(error)
    if not Path(cfg["currency"]["model"]).exists():
        logging.info("Chưa có model tiền; nút giữ lâu sẽ thông báo tính năng chưa sẵn sàng")
    return not errors

def run(cfg, duration=None, mock_button_after=None):
    queues, stop_event, state = make_queues(cfg), threading.Event(), new_state()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop_event.set())
    threads, started = [], time.monotonic()
    health, ready = {name: started for name in WORKERS}, set()
    for name in WORKERS:
        module = importlib.import_module("smart_glass_v2.modules.thread_"+name)
        thread = threading.Thread(target=guarded_run, args=(name, module, queues, stop_event), name=name, daemon=True)
        threads.append(thread)
        thread.start()
    failure, tof, scene = False, None, None
    injected, announced_ready = False, False
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if duration is not None and now-started >= duration:
                break
            if cfg["runtime"]["mock"] and mock_button_after is not None and not injected and now-started >= mock_button_after:
                queues["stm32_queue"].put_nowait({"command": "BTN_SHORT", "time": now, "sequence": 1})
                injected = True
            while True:
                try:
                    item = queues["event_queue"].get_nowait()
                    queues["event_queue"].task_done()
                except queue.Empty:
                    break
                name = item.get("worker")
                if name and item["kind"] in {"ready", "heartbeat"}:
                    health[name] = item["time"]
                    if item["kind"] == "ready":
                        ready.add(name)
                if item["kind"] == "capabilities":
                    state["capabilities"] = item
                    logging.info("Nhãn model: %s; mặt đường=%s; vạch sang đường=%s", item["names"], item["road"], item["crossing"])
                    if not item["road"] or not item["crossing"]:
                        speak(queues, "Chức năng nhận biết vỉa hè và vạch sang đường chưa sẵn sàng.", 3, ttl=30)
                elif item["kind"] == "fatal":
                    failure = True
                elif item["kind"] == "sensor_error" and name == "stm32":
                    announce(state, queues, "button_lost", "Chưa nhận được tín hiệu từ nút bấm. Hãy kiểm tra kết nối.", 2, interval=15)
                elif item["kind"] == "capture_failed" and item["request_id"] == state["request_id"]:
                    state["mode"] = "IDLE"
                    speak(queues, "Không lấy được ảnh, vui lòng thử lại.", 2)
                handle_audio_event(state, item, cfg)
            if not announced_ready and ready == set(WORKERS):
                speak(queues, "Thiết bị đã khởi động. Nhấn nút để đọc chữ, giữ nút rồi thả để nhận diện tiền.", 3, ttl=30)
                announced_ready = True
            for name in WORKERS:
                limit = cfg["runtime"]["worker_timeout_s"] if name in ready else cfg["runtime"]["startup_timeout_s"]
                if now-health[name] > limit:
                    logging.error("Luồng %s không phản hồi trong %.1f giây", name, limit)
                    failure = True
                    stop_event.set()
            for thread in threads:
                if not thread.is_alive():
                    failure = True
                    stop_event.set()
            if state["mode"] != "IDLE" and now > state["deadline"]:
                text = "Đã hết thời gian xác nhận." if state["mode"] == "CROSS_WAIT" else "Yêu cầu chưa hoàn tất, vui lòng thử lại."
                speak(queues, text, 2)
                state.update(mode="IDLE", cross_next=now+cfg["crossing"]["cooldown_s"])
            while True:
                try:
                    button = queues["stm32_queue"].get_nowait()
                    queues["stm32_queue"].task_done()
                except queue.Empty:
                    break
                handle_button(state, queues, button, now)
            while True:
                try:
                    result = queues["result_queue"].get_nowait()
                    queues["result_queue"].task_done()
                except queue.Empty:
                    break
                if state["mode"] in {"OCR_BUSY", "CURRENCY_BUSY"} and result["request_id"] == state["request_id"] and now-result["created"] <= cfg["runtime"]["request_timeout_s"]:
                    speak(queues, result["text"], 2, key=f"request_{result['request_id']}", ttl=30)
                    state["mode"] = "IDLE"
            tof, scene = latest_get(queues["tof_queue"], tof), latest_get(queues["scene_queue"], scene)
            if now-started > 10 and (tof is None or now-tof["time"] > cfg["tof"]["max_age_s"]):
                announce(state, queues, "tof_lost", "Chưa có dữ liệu khoảng cách mới. Hãy kiểm tra cảm biến.", 1, interval=15)
            process_tof(state, queues, tof, now)
            process_scene(state, queues, scene, tof, now)
            stop_event.wait(0.02)
    finally:
        stop_event.set()
        deadline = time.monotonic()+cfg["runtime"]["shutdown_timeout_s"]
        for thread in threads:
            thread.join(max(0, deadline-time.monotonic()))
        stuck = [thread.name for thread in threads if thread.is_alive()]
        if stuck:
            logging.error("Luồng chưa dừng: %s; systemd sẽ thu hồi tiến trình", stuck)
            failure = True
        # Một luồng lỗi có thể đặt stop_event trước khi coordinator đọc sự kiện fatal.
        while True:
            try:
                if queues["event_queue"].get_nowait()["kind"] == "fatal":
                    failure = True
                queues["event_queue"].task_done()
            except queue.Empty:
                break
    return 1 if failure else 0

def main():
    parser = argparse.ArgumentParser(description="Kính hỗ trợ quan sát, bản thứ hai")
    parser.add_argument("--config")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--mock-button-after", type=float)
    parser.add_argument("--model")
    parser.add_argument("--port")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
    os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT/".cache"/"ultralytics"))
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    cfg = load_config(args.config, args.mock)
    if args.model:
        cfg["vision"]["model"] = str(Path(args.model).resolve())
    if args.port:
        cfg["stm32"]["port"] = args.port
    if args.check:
        return 0 if check_files(cfg) else 1
    if not cfg["runtime"]["mock"] and not check_files(cfg):
        return 1
    return run(cfg, args.duration, args.mock_button_after)

if __name__ == "__main__":
    raise SystemExit(main())
