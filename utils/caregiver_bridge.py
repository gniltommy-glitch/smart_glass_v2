"""Tiện ích cầu nối Caregiver trên kính (chụp ảnh tĩnh JPEG nén & đọc lời nhắn).

Tách riêng để không can thiệp vào luồng YOLO, VietOCR hay giao thức UART hiện hữu.
"""
import base64
from datetime import datetime, timezone
import io
import queue
import secrets
import time
import numpy as np

from .channels import notify_app, speak


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode_jpeg_base64(frame, quality=78):
    """Mã hóa mảng ảnh BGR/RGB NumPy sang chuỗi JPEG base64 nhẹ (~50-150 KB)."""
    if frame is None or not isinstance(frame, np.ndarray) or frame.ndim < 2:
        raise ValueError("Khung hình camera không hợp lệ")

    height, width = int(frame.shape[0]), int(frame.shape[1])

    # Ưu tiên OpenCV (nhanh trên Pi 5, đúng thứ tự kênh BGR của Picamera2)
    try:
        import cv2
        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)],
        )
        if ok:
            return base64.b64encode(buf.tobytes()).decode("ascii"), width, height
    except Exception:
        pass

    # Dự phòng bằng Pillow (hữu ích khi chạy --mock trên máy chưa cài cv2)
    from PIL import Image
    rgb = frame[..., ::-1] if frame.ndim == 3 and frame.shape[2] == 3 else frame
    img = Image.fromarray(np.ascontiguousarray(rgb))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=int(quality))
    return base64.b64encode(out.getvalue()).decode("ascii"), width, height


def peek_latest_snapshot_frame(queues):
    """Đọc khung hình mới nhất từ snapshot_frame_queue mà không làm đói luồng khác."""
    snap_q = queues.get("snapshot_frame_queue")
    item = None
    if snap_q is not None:
        try:
            item = snap_q.get_nowait()
            snap_q.task_done()
            # Giữ lại bản sao trong hàng đợi 1 phần tử để lần gọi sát nhau vẫn có ảnh
            try:
                snap_q.put_nowait(item)
            except queue.Full:
                pass
        except queue.Empty:
            item = None

    if item is None and queues.get("config", {}).get("runtime", {}).get("mock"):
        cfg_cam = queues["config"].get("camera", {})
        w, h = cfg_cam.get("size", (640, 360))
        mock_frame = np.zeros((h, w, 3), dtype=np.uint8)
        mock_frame[:, :] = (36, 28, 20)
        item = {"frame": mock_frame, "time": time.monotonic(), "id": 0}

    return item


def capture_caregiver_snapshot(queues, question="", context_note="", broadcast=False):
    """Đóng gói ảnh chụp tĩnh JPEG base64 cho tính năng 'Hỏi người thân' / SOS."""
    item = peek_latest_snapshot_frame(queues)
    if item is None or item.get("frame") is None:
        return None

    b64, width, height = encode_jpeg_base64(item["frame"])
    req_id = f"glass-{secrets.token_hex(6)}"
    payload = {
        "type": "assist_request",
        "request_id": req_id,
        "question": (question or "Người đeo kính gửi ảnh nhờ kiểm tra phía trước.")[:300],
        "context_note": (context_note or "")[:300],
        "image_base64": b64,
        "width": width,
        "height": height,
        "frame_id": item.get("id", 0),
        "created_at": utc_now_iso(),
    }
    if broadcast:
        notify_app(queues, payload)
    return payload


def enqueue_caregiver_speech(queues, reply_text, sender_name="Người thân", request_id=""):
    """Đưa câu trả lời của người thân vào hàng đợi âm thanh Piper -> STM32 -> tai nghe."""
    clean_text = str(reply_text or "").strip()
    if not clean_text:
        return None
    clean_sender = str(sender_name or "Người thân").strip()[:60]
    spoken = f"{clean_sender} nhắn: {clean_text[:350]}"
    key = f"caregiver_{request_id or secrets.token_hex(4)}"
    return speak(queues, spoken, priority=2, key=key, ttl=25.0)
