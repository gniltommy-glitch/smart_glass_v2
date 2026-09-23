"""YOLO chạy trên ảnh mới nhất; kết quả kèm thời gian và bằng chứng vùng mặt đường."""
from pathlib import Path
import queue
import time
import numpy as np
from ..utils.channels import beat, event, latest_put

def lower_coverage(detections, names, width, height):
    import cv2
    mask = np.zeros((height, width), dtype=np.uint8)
    for item in detections:
        if item["label"] not in names:
            continue
        polygon = item.get("polygon")
        if polygon is not None and len(polygon) >= 3:
            cv2.fillPoly(mask, [np.asarray(polygon, dtype=np.int32)], 1)
        else:
            x1, y1, x2, y2 = item["bbox"]
            x1, x2 = max(0, int(x1)), min(width, int(x2))
            y1, y2 = max(0, int(y1)), min(height, int(y2))
            mask[y1:y2, x1:x2] = 1
    return float(mask[int(height*0.7):].mean())

def camera_motion(previous, current):
    import cv2
    if previous is None or previous.shape != current.shape:
        return None
    points = cv2.goodFeaturesToTrack(previous, maxCorners=80, qualityLevel=0.02, minDistance=8)
    if points is None or len(points) < 12:
        return None
    tracked, status, _ = cv2.calcOpticalFlowPyrLK(previous, current, points, None)
    if tracked is None or status is None:
        return None
    valid = status.reshape(-1).astype(bool)
    if valid.sum() < 10:
        return None
    return float(np.median(np.linalg.norm(tracked[valid]-points[valid], axis=2)))

def run_thread(queues, stop_event):
    cfg, labels = queues["config"]["vision"], queues["config"]["labels"]
    mock = queues["config"]["runtime"]["mock"]
    model, previous_gray = None, None
    if not mock:
        import cv2
        import torch
        from ultralytics import YOLO
        cv2.setNumThreads(1)
        torch.set_num_threads(cfg["threads"])
        if not Path(cfg["model"]).exists():
            raise FileNotFoundError(f"Chưa có model YOLO cục bộ: {cfg['model']}")
        model = YOLO(cfg["model"])
        names = {str(name).lower() for name in model.names.values()}
    else:
        names = {"person", "car", "road", "sidewalk", "zebra_crossing"}
    event(queues, "capabilities", names=sorted(names),
          road=bool(names.intersection(labels["road"]) and names.intersection(labels["sidewalk"])),
          crossing=bool(names.intersection(labels["zebra"]) and names.intersection(labels["vehicles"])))
    event(queues, "ready", worker="vision")
    while not stop_event.is_set():
        beat(queues, "vision")
        try:
            frame = queues["vision_queue"].get(timeout=0.1)
            queues["vision_queue"].task_done()
        except queue.Empty:
            continue
        if time.monotonic()-frame["time"] > queues["config"]["camera"]["max_age_s"]:
            continue
        started = time.monotonic()
        height, width = frame["frame"].shape[:2]
        detections = []
        if mock:
            detections = [{"label": "person", "confidence": 0.9,
                           "bbox": [width*.4, height*.25, width*.6, height*.9]}]
            motion = 0.0
        else:
            result = model.predict(frame["frame"], imgsz=cfg["imgsz"], conf=cfg["confidence"],
                                   device="cpu", verbose=False)[0]
            if result.boxes is not None:
                polygons = result.masks.xy if result.masks is not None else None
                for index, box in enumerate(result.boxes):
                    item = {"label": str(model.names[int(box.cls.item())]).lower(),
                            "confidence": float(box.conf.item()), "bbox": box.xyxy[0].tolist()}
                    if polygons is not None and index < len(polygons):
                        item["polygon"] = polygons[index].tolist()
                    detections.append(item)
            gray = cv2.cvtColor(frame["frame"], cv2.COLOR_BGR2GRAY)
            motion = camera_motion(previous_gray, gray)
            previous_gray = gray
        vehicles = [{"label": d["label"], "bbox": [d["bbox"][0]/width, d["bbox"][1]/height,
                      d["bbox"][2]/width, d["bbox"][3]/height]}
                    for d in detections if d["label"] in labels["vehicles"]]
        zebras = [d for d in detections if d["label"] in labels["zebra"] and
                  d["bbox"][3] > height*.5 and d["bbox"][0] < width*.7 and d["bbox"][2] > width*.3]
        street, sidewalk = (0.0, 0.0) if mock else (
            lower_coverage(detections, labels["road"], width, height),
            lower_coverage(detections, labels["sidewalk"], width, height))
        latest_put(queues["scene_queue"], {"id": frame["id"], "time": frame["time"],
            "size": (width, height), "detections": detections, "vehicles": vehicles,
            "camera_motion": motion, "zebra": bool(zebras), "road_coverage": street,
            "sidewalk_coverage": sidewalk})
        stop_event.wait(max(0, 1/cfg["fps"]-(time.monotonic()-started)))
