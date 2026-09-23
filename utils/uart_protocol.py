"""Giao thức UART nhị phân v2: PCM và nút dùng chung đường truyền có CRC16."""
import binascii
import struct
import time

START, PCM, STOP, BUTTON_ACK = 0x10, 0x11, 0x12, 0x20
ACK, BUTTON, STATUS, DONE, BOOT = 0x80, 0x81, 0x83, 0x84, 0x85
MAX_PAYLOAD = 1024

def encode(kind, sequence, payload=b""):
    if not 0 <= sequence <= 65535 or len(payload) > MAX_PAYLOAD:
        raise ValueError("Số thứ tự hoặc độ dài gói UART không hợp lệ")
    body = struct.pack("<BHH", kind, sequence, len(payload))+payload
    return b"\xa5\x5a"+body+struct.pack("<H", binascii.crc_hqx(body, 0xffff))

def new_decoder():
    return {"buffer": bytearray(), "time": 0.0, "errors": 0}

def feed(state, data, now=None):
    now = time.monotonic() if now is None else now
    buf = state["buffer"]
    if buf and now-state["time"] > .1:
        buf.clear()
    if data:
        state["time"] = now
    buf.extend(data)
    frames = []
    while buf:
        start = buf.find(b"\xa5\x5a")
        if start < 0:
            buf[:] = b"\xa5" if buf[-1] == 0xa5 else b""
            break
        del buf[:start]
        if len(buf) < 7:
            break
        kind, sequence, length = struct.unpack_from("<BHH", buf, 2)
        if length > MAX_PAYLOAD:
            del buf[0]
            state["errors"] += 1
            continue
        if len(buf) < length+9:
            break
        checksum = struct.unpack_from("<H", buf, length+7)[0]
        if checksum != binascii.crc_hqx(buf[2:length+7], 0xffff):
            del buf[0]
            state["errors"] += 1
            continue
        frames.append((kind, sequence, bytes(buf[7:length+7])))
        del buf[:length+9]
    return frames

def parse_status(payload):
    if len(payload) != 40:
        raise ValueError("Cần firmware âm thanh v2; độ dài STATUS không tương thích")
    values = struct.unpack("<4B9I", payload)
    names = ("version", "audio_ok", "audio_active", "reserved", "uptime_ms", "underruns",
             "uart_errors", "rx_overflows", "crc_errors", "samples_played", "play_id",
             "button_count", "exti_edges")
    status = dict(zip(names, values))
    if status["version"] != 2:
        raise ValueError("Cần nạp firmware stm32_glass_audio v2")
    return status
