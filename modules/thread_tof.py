"""Đọc SEN0628 qua RP2040 trên /dev/i2c-1, không truy cập thanh ghi VL53L7CX trần."""
import logging
import struct
import time
import numpy as np
from ..utils.channels import beat, event, latest_put
from ..utils.spatial_matcher import normalize_matrix

def read_reply(read, command, stop_event, timeout=2.0):
    deadline = time.monotonic()+timeout
    while not stop_event.is_set() and time.monotonic() < deadline:
        status = read(1)[0]
        if status in (0x53, 0x63):
            echoed = read(1)[0]
            length = struct.unpack("<H", read(2))[0]
            if length > 256:
                raise ValueError("Độ dài phản hồi SEN0628 vượt giới hạn")
            payload = bytearray()
            while len(payload) < length:
                payload.extend(read(min(32, length-len(payload))))
            if status != 0x53 or echoed != command:
                raise ValueError(f"SEN0628 trả lỗi hoặc lệch lệnh: {status:02x}/{echoed:02x}")
            return bytes(payload)
        stop_event.wait(0.017)
    raise TimeoutError("Hết thời gian đợi phản hồi SEN0628")

def run_thread(queues, stop_event):
    cfg = queues["config"]["tof"]
    bus, sequence = None, 0
    mock = queues["config"]["runtime"]["mock"]
    if not mock:
        from smbus2 import SMBus, i2c_msg
    event(queues, "ready", worker="tof")
    while not stop_event.is_set():
        beat(queues, "tof")
        try:
            if mock:
                raw = np.full((8, 8), 2200, dtype=np.uint16)
                raw[3:5, 3:5] = 1400
            else:
                if bus is None:
                    bus = SMBus(cfg["bus"])
                    if stop_event.wait(cfg["boot_wait_s"]):
                        break
                    def read(n):
                        message = i2c_msg.read(cfg["address"], n)
                        bus.i2c_rdwr(message)
                        return bytes(message)
                    bus.i2c_rdwr(i2c_msg.write(cfg["address"], bytes.fromhex("55 00 05 01 00 00 00 08")))
                    stop_event.wait(0.017)
                    read_reply(read, 1, stop_event, cfg["reply_timeout_s"])
                    if stop_event.wait(cfg["mode_wait_s"]):
                        break
                bus.i2c_rdwr(i2c_msg.write(cfg["address"], bytes.fromhex("55 00 01 02")))
                stop_event.wait(0.017)
                payload = read_reply(read, 2, stop_event, cfg["reply_timeout_s"])
                if len(payload) != 128:
                    raise ValueError(f"Cần 128 byte cho ma trận, nhận {len(payload)}")
                raw = np.asarray(struct.unpack("<64H", payload)).reshape(8, 8)
            sequence += 1
            latest_put(queues["tof_queue"], {"matrix": normalize_matrix(raw, cfg),
                       "time": time.monotonic(), "id": sequence})
        except (OSError, ValueError, TimeoutError) as exc:
            event(queues, "sensor_error", worker="tof", message=str(exc))
            logging.warning("ToF: %s", exc)
            if bus is not None:
                bus.close()
                bus = None
            stop_event.wait(1.0)
        stop_event.wait(cfg["period_s"])
    if bus is not None:
        bus.close()
