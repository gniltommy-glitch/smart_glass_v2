"""Phát hiện dòng chữ bằng DBNet/RapidOCR, nhận dạng VietOCR và nhận diện tiền tùy model."""
from pathlib import Path
import logging
import queue
import time
import numpy as np
from ..utils.channels import beat, event
from ..utils.text_postprocess import clean_text

def load_ocr(cfg):
    import yaml
    from rapidocr_onnxruntime import RapidOCR
    from vietocr.tool.predictor import Predictor
    for key in ("detector", "config", "weights"):
        if not Path(cfg[key]).is_file():
            raise FileNotFoundError(f"Thiếu model/cấu hình OCR: {cfg[key]}")
    settings = yaml.safe_load(Path(cfg["config"]).read_text(encoding="utf-8"))
    if not all(key in settings for key in ("vocab", "backbone", "cnn", "dataset")):
        raise ValueError("Cấu hình VietOCR phải gộp đầy đủ base.yml và cấu hình kiến trúc")
    settings.update(device="cpu", weights=cfg["weights"])
    settings["cnn"]["pretrained"] = False
    settings.setdefault("predictor", {})["beamsearch"] = False
    recognizer = Predictor(settings)
    detector = RapidOCR(det_model_path=cfg["detector"], intra_op_num_threads=cfg["threads"],
                        inter_op_num_threads=1, use_cls=False, use_rec=False)
    return detector, recognizer

def read_text(frame, detector, recognizer, cfg, stop_event, progress=lambda: None):
    import cv2
    from PIL import Image
    height, width = frame.shape[:2]
    scale = min(1, cfg["max_side"]/max(height, width))
    if scale < 1:
        frame = cv2.resize(frame, (round(width*scale), round(height*scale)))
    boxes, _ = detector(frame, use_det=True, use_cls=False, use_rec=False)
    if boxes is None:
        return ""
    boxes = sorted(boxes, key=lambda box: (min(p[1] for p in box), min(p[0] for p in box)))
    lines = []
    for box in boxes[:cfg["max_lines"]]:
        if stop_event.is_set():
            break
        progress()
        points = np.asarray(box, dtype=np.float32)
        if points.shape != (4, 2):
            continue
        width = max(2, round(max(np.linalg.norm(points[1]-points[0]), np.linalg.norm(points[2]-points[3]))))
        height = max(2, round(max(np.linalg.norm(points[3]-points[0]), np.linalg.norm(points[2]-points[1]))))
        target = np.array([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]], np.float32)
        crop = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(points, target), (width, height))
        text, confidence = recognizer.predict(Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)), return_prob=True)
        if confidence is not None and float(confidence) >= cfg["min_confidence"]:
            lines.append(str(text))
    return clean_text("\n".join(lines), cfg["replacements"], cfg["max_chars"])

def read_currency(frame, model, cfg):
    height, width = frame.shape[:2]
    roi = frame[int(height*.15):int(height*.85), int(width*.15):int(width*.85)]
    result = model.predict(roi, device="cpu", verbose=False, conf=cfg["confidence"])[0]
    candidates = []
    if result.probs is not None:
        candidates.append((float(result.probs.top1conf), str(model.names[result.probs.top1])))
    elif result.boxes is not None:
        candidates = [(float(b.conf.item()), str(model.names[int(b.cls.item())])) for b in result.boxes]
    valid = [(confidence, cfg["labels"][name]) for confidence, name in candidates
             if confidence >= cfg["confidence"] and name in cfg["labels"]]
    amounts = {amount for _, amount in valid}
    if len(amounts) == 1:
        return "Tờ tiền có thể là " + next(iter(amounts)) + "."
    return "Chưa xác định được mệnh giá. Hãy đưa một tờ tiền rõ hơn vào giữa camera."

def run_thread(queues, stop_event):
    cfg = queues["config"]
    detector = recognizer = currency_model = None
    event(queues, "ready", worker="ocr")
    while not stop_event.is_set():
        beat(queues, "ocr")
        try:
            item = queues["ocr_queue"].get(timeout=0.1)
        except queue.Empty:
            continue
        response = {"request_id": item["request_id"], "kind": item["kind"], "created": item["created"]}
        try:
            if time.monotonic()-item["created"] > cfg["runtime"]["request_timeout_s"]:
                response["text"] = "Yêu cầu đã hết thời gian. Vui lòng bấm nút lại."
            elif cfg["runtime"]["mock"]:
                response["text"] = "Đây là văn bản mô phỏng tiếng Việt." if item["kind"] == "ocr" else "Đây là yêu cầu nhận diện tiền mô phỏng."
            elif item["kind"] == "ocr":
                if detector is None:
                    detector, recognizer = load_ocr(cfg["ocr"])
                text = read_text(item["frame"], detector, recognizer, cfg["ocr"], stop_event,
                                 lambda: beat(queues, "ocr"))
                response["text"] = text or "Không đọc được chữ rõ ràng. Hãy giữ camera ổn định và thử lại."
            else:
                if not Path(cfg["currency"]["model"]).exists():
                    response["text"] = "Chức năng nhận diện tiền chưa sẵn sàng."
                else:
                    if currency_model is None:
                        from ultralytics import YOLO
                        currency_model = YOLO(cfg["currency"]["model"])
                    response["text"] = read_currency(item["frame"], currency_model, cfg["currency"])
        except Exception as exc:
            logging.exception("Không xử lý được yêu cầu %s", item["kind"])
            response.update(text="Chưa đọc được ảnh. Vui lòng giữ camera ổn định và thử lại.", error=str(exc))
        finally:
            queues["ocr_queue"].task_done()
        while not stop_event.is_set():
            try:
                queues["result_queue"].put(response, timeout=0.1)
                break
            except queue.Full:
                beat(queues, "ocr")
