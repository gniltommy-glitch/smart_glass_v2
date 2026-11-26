"""Chỉ luồng này mở IMX708; ảnh lớn và ảnh YOLO lấy từ cùng một camera."""
import queue
import time
import numpy as np
from ..utils.channels import beat, event, latest_put

def run_thread(queues, stop_event):
    cfg, mock = queues["config"]["camera"], queues["config"]["runtime"]["mock"]
    camera, frame_id = None, 0
    if not mock:
        import cv2
        from picamera2 import Picamera2
        from libcamera import controls
        camera = Picamera2()
        camera.configure(camera.create_video_configuration(
            main={"size": tuple(cfg["capture_size"]), "format": "RGB888"},
            lores={"size": tuple(cfg["size"]), "format": "YUV420"},
            controls={"FrameRate": cfg["fps"], "AfMode": controls.AfModeEnum.Continuous},
            buffer_count=3, queue=False))
        camera.start()
    event(queues, "ready", worker="camera")
    try:
        while not stop_event.is_set():
            beat(queues, "camera")
            try:
                capture = queues["camera_requests"].get_nowait()
                queues["camera_requests"].task_done()
            except queue.Empty:
                capture = None
            if mock:
                width, height = cfg["size"]
                small = np.zeros((height, width, 3), dtype=np.uint8)
                large = small.copy() if capture else None
                timestamp = time.monotonic()
            else:
                request = camera.capture_request()
                try:
                    timestamp = time.monotonic()
                    small = cv2.cvtColor(request.make_array("lores"), cv2.COLOR_YUV2BGR_I420)
                    # RGB888 của Picamera2 được sắp byte BGR, dùng trực tiếp với OpenCV.
                    large = request.make_array("main").copy() if capture else None
                finally:
                    request.release()
            frame_id += 1
            latest_put(queues["vision_queue"], {"frame": small, "time": timestamp, "id": frame_id})
            if "snapshot_frame_queue" in queues:
                latest_put(
                    queues["snapshot_frame_queue"],
                    {
                        "frame": large if large is not None else small,
                        "time": timestamp,
                        "id": frame_id,
                    },
                )
            if capture:
                try:
                    queues["ocr_queue"].put_nowait({**capture, "frame": large, "time": timestamp})
                except queue.Full:
                    event(queues, "capture_failed", request_id=capture["request_id"])
            if mock:
                stop_event.wait(1/cfg["fps"])
    finally:
        if camera is not None:
            camera.stop()
            camera.close()
