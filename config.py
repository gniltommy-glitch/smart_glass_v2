"""Cấu hình tập trung; mọi đường dẫn tương đối tính từ smart_glass_v2."""
from copy import deepcopy
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent
DEFAULT = {
    "camera": {"size": [640, 360], "capture_size": [2304, 1296], "fps": 10,
               "hfov_deg": 75.0, "max_age_s": 0.7},
    "tof": {"bus": 1, "address": 0x33, "period_s": 0.1, "reply_timeout_s": 2.0,
            "boot_wait_s": 3.0, "mode_wait_s": 5.0, "max_age_s": 0.5,
            "min_mm": 20, "invalid_from_mm": 4000,
            "hfov_deg": 60.0, "vfov_deg": 60.0,
            "rotate_quarters": 0, "flip_x": False, "flip_y": False,
            "yaw_deg": 0.0, "pitch_deg": 0.0, "calibrated": False,
            "fusion_skew_s": 0.20, "center_fraction": 0.35},
    "vision": {"model": "models/yolo.pt", "imgsz": 416, "confidence": 0.45,
               "threads": 2, "fps": 5, "result_max_age_s": 1.0},
    "navigation": {"near_m": 0.7, "near_cells": 4, "crowd_objects": 5,
                   "clear_m": 1.4, "min_columns": 2, "min_valid_fraction": 0.8,
                   "road_coverage": 0.50, "sidewalk_coverage": 0.15},
    "crossing": {"confirm_s": 5.0, "frames": 10, "max_gap_s": 0.8,
                 "observation_timeout_s": 12.0, "speed_norm_s": 0.015,
                 "total_motion_norm": 0.025, "scale_change": 0.05,
                 "max_camera_motion_px": 2.5, "cooldown_s": 20.0},
    "ocr": {"detector": "models/ocr/text_detector.onnx",
            "config": "models/ocr/vietocr.yml", "weights": "models/ocr/vietocr.pth",
            "threads": 1, "min_confidence": 0.65, "max_lines": 30,
            "max_chars": 1400, "max_side": 1800, "replacements": {}},
    "currency": {"model": "models/currency.pt", "confidence": 0.80,
                 "labels": {"vnd_1000": "một nghìn đồng", "vnd_2000": "hai nghìn đồng",
                            "vnd_5000": "năm nghìn đồng", "vnd_10000": "mười nghìn đồng",
                            "vnd_20000": "hai mươi nghìn đồng", "vnd_50000": "năm mươi nghìn đồng",
                            "vnd_100000": "một trăm nghìn đồng", "vnd_200000": "hai trăm nghìn đồng",
                            "vnd_500000": "năm trăm nghìn đồng"}},
    "stm32": {"port": "/dev/ttyAMA0", "baud": 1000000, "heartbeat_s": 3.0,
              "command_timeout_s": 3.0},
    "audio": {"model": "models/piper/vi_VN-vais1000-medium.onnx",
              "synthesis_timeout_s": 35.0,
              "playback_timeout_s": 60.0, "cache_entries": 16},
    "runtime": {"startup_timeout_s": 120, "worker_timeout_s": 90,
                "shutdown_timeout_s": 5, "request_timeout_s": 60,
                "repeat_s": 5.0, "mock": False},
    "labels": {
        "road": ["street", "road", "roadway"], "sidewalk": ["sidewalk", "walkway", "pavement"],
        "zebra": ["zebra crossing", "zebra_crossing", "crosswalk", "pedestrian_crossing"],
        "vehicles": ["car", "motorbike", "motorcycle", "bus", "truck", "vehicle"],
        "vi": {"person": "người", "chair": "ghế", "car": "ô tô", "motorbike": "xe máy",
               "motorcycle": "xe máy", "bus": "xe buýt", "truck": "xe tải", "vehicle": "xe",
               "bicycle": "xe đạp", "tree": "cây", "pole": "cột", "building": "tòa nhà",
               "cabinet": "tủ", "fence": "hàng rào", "sign": "biển báo",
               "curb_obstacle": "gờ bó vỉa", "street_obstacle": "vật cản"}},
}

def load_config(path=None, mock=False):
    cfg = deepcopy(DEFAULT)
    def merge(dst, src):
        for key, value in src.items():
            if key not in dst:
                raise ValueError(f"Khóa cấu hình không hợp lệ: {key}")
            if isinstance(value, dict) and isinstance(dst[key], dict):
                if key in {"vi", "replacements"} or (key == "labels" and dst is cfg["currency"]):
                    dst[key].update(value)
                else:
                    merge(dst[key], value)
            else:
                dst[key] = value
    if path:
        merge(cfg, json.loads(Path(path).read_text(encoding="utf-8")))
    cfg["runtime"]["mock"] = mock
    for section, keys in {"vision": ["model"], "ocr": ["detector", "config", "weights"],
                          "currency": ["model"], "audio": ["model"]}.items():
        for key in keys:
            cfg[section][key] = str((ROOT / cfg[section][key]).resolve())
    if not 0x30 <= cfg["tof"]["address"] <= 0x33:
        raise ValueError("SEN0628 cần địa chỉ 7-bit từ 0x30 đến 0x33")
    for section, key in [("camera", "fps"), ("vision", "fps"), ("tof", "period_s")]:
        if cfg[section][key] <= 0:
            raise ValueError(f"{section}.{key} phải dương")
    for section, key in [("camera", "hfov_deg"), ("tof", "hfov_deg"), ("tof", "vfov_deg")]:
        if not 0 < cfg[section][key] < 180:
            raise ValueError(f"Góc nhìn {section}.{key} không hợp lệ")
    if not 0 < cfg["tof"]["center_fraction"] <= 1:
        raise ValueError("tof.center_fraction phải trong khoảng (0, 1]")
    if not 1 <= cfg["navigation"]["min_columns"] <= 8 or not 0 < cfg["navigation"]["min_valid_fraction"] <= 1:
        raise ValueError("Cấu hình hành lang ToF không hợp lệ")
    if cfg["audio"]["cache_entries"] < 1 or cfg["crossing"]["frames"] < 2:
        raise ValueError("Cần ít nhất một mục cache và hai khung hình theo dõi")
    return cfg
