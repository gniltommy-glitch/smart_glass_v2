"""Test riêng ToF, nút STM32, camera hoặc Piper; không cần bật cả hệ thống AI."""
import argparse
import importlib
import logging
from pathlib import Path
import queue
import sys
import threading
import time
import wave
import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from smart_glass_v2.config import ROOT, load_config
from smart_glass_v2.utils.channels import make_queues, guarded_run, latest_get, speak

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("tof", "button", "camera", "audio"))
    parser.add_argument("--config")
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--port")
    audio_input = parser.add_mutually_exclusive_group()
    audio_input.add_argument("--tone", action="store_true", help="Phát tiếng 440 Hz qua jack STM32, không cần model")
    audio_input.add_argument("--wav", help="Phát file WAV/MP3 qua STM32; tự chuyển sang PCM 16 kHz")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config, args.mock)
    if args.port:
        cfg["stm32"]["port"] = args.port
    cfg["audio"]["playback_timeout_s"] = max(60, args.duration)
    queues, stop = make_queues(cfg), threading.Event()
    name = "stm32" if args.component == "button" else args.component
    threads = []
    for worker in (["stm32", "audio"] if name == "audio" else [name]):
        module = importlib.import_module("smart_glass_v2.modules.thread_"+worker)
        thread = threading.Thread(target=guarded_run, args=(worker, module, queues, stop), daemon=True)
        thread.start()
        threads.append(thread)
    success, failed, requested = False, False, False
    started, last_display = time.monotonic(), 0.0
    if name == "audio":
        source = args.wav
        if args.tone:
            folder = ROOT/".cache"
            folder.mkdir(exist_ok=True)
            source = folder/"tone.wav"
            samples = (2000*np.sin(2*np.pi*440*np.arange(16000)/16000)).astype("<i2")
            with wave.open(str(source), "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(samples.tobytes())
        speak(queues, "Xin chào. Đây là kiểm tra âm thanh qua tai nghe cắm vào bo STM32.", 2, ttl=60, file=source)
    if name == "stm32":
        print("Nhấn ngắn rồi giữ nút xanh ít nhất 1,2 giây và thả. Mỗi lần chỉ được hiện một sự kiện.")
    try:
        while not stop.is_set() and time.monotonic()-started < args.duration:
            now = time.monotonic()
            if name == "camera" and not requested and now-started >= 1:
                queues["camera_requests"].put_nowait({"kind": "ocr", "request_id": 1, "created": now})
                requested = True
            if name == "tof":
                sample = latest_get(queues["tof_queue"])
                if sample is not None and now-last_display >= .5:
                    print("Ma trận mét; -- là chưa đo được:")
                    for row in sample["matrix"]:
                        print(" ".join(f"{v:4.2f}" if np.isfinite(v) else "  --" for v in row))
                    print(f"Ô hợp lệ: {np.isfinite(sample['matrix']).sum()}/64")
                    success = True
                    last_display = now
            elif name == "stm32":
                if args.mock and not requested and now-started >= 1:
                    queues["stm32_queue"].put_nowait({"command": "BTN_SHORT", "sequence": 1, "time": now})
                    requested = True
                while not queues["stm32_queue"].empty():
                    item = queues["stm32_queue"].get_nowait()
                    queues["stm32_queue"].task_done()
                    print(item["command"], "số thứ tự", item["sequence"])
                    success = True
            elif name == "camera":
                latest_get(queues["vision_queue"])
                capture = latest_get(queues["ocr_queue"])
                if capture is not None:
                    import cv2
                    output = ROOT/"test_camera.jpg"
                    if not cv2.imwrite(str(output), capture["frame"]):
                        raise OSError("Không ghi được ảnh kiểm tra")
                    print("Đã chụp:", output, "kích thước", capture["frame"].shape)
                    success = True
                    break
            while True:
                try:
                    event = queues["event_queue"].get_nowait()
                    queues["event_queue"].task_done()
                except queue.Empty:
                    break
                if event["kind"] == "fatal":
                    failed = True
                elif event["kind"] == "sensor_error":
                    logging.warning(event["message"])
                elif event["kind"] == "stm32_status":
                    print("Codec:", event["audio_ok"], "đang phát:", event["audio_active"],
                          "thiếu mẫu:", event["underruns"], "lỗi UART:", event["uart_errors"],
                          "tràn RX:", event["rx_overflows"])
                elif event["kind"] == "audio_done":
                    success = True
            if name == "audio" and success:
                break
            stop.wait(.03)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for thread in threads:
            thread.join(cfg["runtime"]["shutdown_timeout_s"])
            if thread.is_alive():
                failed = True
        if any(item["kind"] == "fatal" for item in list(queues["event_queue"].queue)):
            failed = True
    print("Test mô phỏng." if args.mock else "Test thiết bị thật.")
    print("Đã nhận được đầu ra." if success and not failed else "Chưa đạt; xem lỗi và kiểm tra lại kết nối.")
    return 0 if success and not failed else 1

if __name__ == "__main__":
    raise SystemExit(main())
