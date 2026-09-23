"""Chủ sở hữu UART duy nhất: gửi PCM, nhận ACK/DONE và nhận nút ngay trong lúc phát."""
import logging
import queue
import secrets
import struct
import time
from ..utils.channels import beat, event, latest_put
from ..utils.uart_protocol import (encode, feed, new_decoder, parse_status,
                                   ACK, BUTTON, BUTTON_ACK, STATUS, DONE, BOOT, STOP)

def write_packet(port, kind, sequence, payload=b""):
    data = encode(kind, sequence, payload)
    if port.write(data) != len(data):
        raise OSError("UART chưa ghi đủ gói dữ liệu")

def dispatch(queues, port, state, frame):
    kind, seq, payload = frame
    now = time.monotonic()
    state["last_rx"] = now
    if kind == ACK and len(payload) == 1:
        return seq, payload[0]
    if kind == BUTTON and len(payload) == 5:
        count, action = struct.unpack("<IB", payload)
        if action not in (1, 2):
            return None
        if count != state["last_button"]:
            try:
                queues["stm32_queue"].put_nowait({"command": "BTN_SHORT" if action == 1 else "BTN_CURRENCY",
                                                "sequence": count, "time": now})
            except queue.Full:
                return None
            state["last_button"] = count
        write_packet(port, BUTTON_ACK, seq, struct.pack("<I", count))
    elif kind == STATUS:
        status = parse_status(payload)
        if state["status"] and status["uptime_ms"] < state["status"]["uptime_ms"]:
            state["generation"] += 1
            state["last_button"] = None
            event(queues, "stm32_reset")
        state["status"] = status
        latest_put(queues["audio_status_queue"], {**status, "time": now})
        event(queues, "stm32_status", **status)
        # Heartbeat là phương án xác nhận dự phòng khi gói DONE bị mất.
        if status["play_id"] and not status["audio_active"]:
            latest_put(queues["audio_done_queue"], {"play_id": status["play_id"], "samples": status["samples_played"]})
    elif kind == DONE and len(payload) == 8:
        play_id, samples = struct.unpack("<II", payload)
        latest_put(queues["audio_done_queue"], {"play_id": play_id, "samples": samples})
    elif kind == BOOT:
        state.update(last_button=None, status=None, generation=state["generation"]+1)
        event(queues, "stm32_reset")
    return None

def receive(queues, port, state):
    acks = []
    for frame in feed(state["decoder"], port.read(2048)):
        ack = dispatch(queues, port, state, frame)
        if ack is not None:
            acks.append(ack)
    return acks

def execute(queues, port, state, job, stop_event):
    state["sequence"] = (state["sequence"]+1) & 0xffff
    sequence, generation = state["sequence"], state["generation"]
    next_send = 0.0
    while not stop_event.is_set() and time.monotonic() < job["deadline"]:
        beat(queues, "stm32")
        if job["cancel"].is_set():
            raise InterruptedError("Đã hủy gói âm thanh")
        if generation != state["generation"]:
            raise OSError("STM32 reset giữa lúc truyền âm thanh")
        if time.monotonic() >= next_send:
            write_packet(port, job["kind"], sequence, job["payload"])
            next_send = time.monotonic()+.15
        acks = receive(queues, port, state)
        if generation != state["generation"]:
            raise OSError("STM32 reset giữa lúc truyền âm thanh")
        for ack_seq, status in acks:
            if ack_seq != sequence:
                continue
            if status == 0:
                return
            if status == 1:
                # Ring PCM đầy: đợi ít rồi gửi lại cùng số thứ tự, không bỏ mẫu.
                next_send = time.monotonic()+.008
            else:
                raise OSError(f"STM32 từ chối gói: mã {status}; 2=sai dữ liệu, 3=codec chưa hoạt động")
    raise TimeoutError("Không nhận ACK âm thanh từ STM32")

def new_link_state():
    return {"decoder": new_decoder(), "last_button": None, "status": None,
            "generation": 0, "sequence": secrets.randbelow(65536), "last_rx": time.monotonic()}

def run_thread(queues, stop_event):
    cfg = queues["config"]["stm32"]
    if queues["config"]["runtime"]["mock"]:
        event(queues, "ready", worker="stm32")
        while not stop_event.is_set():
            beat(queues, "stm32")
            stop_event.wait(.1)
        return
    import serial
    port, state, next_warning = None, new_link_state(), 0.0
    event(queues, "ready", worker="stm32")
    try:
        while not stop_event.is_set():
            beat(queues, "stm32")
            try:
                if port is None:
                    port = serial.Serial(cfg["port"], cfg["baud"], timeout=.002, write_timeout=.2)
                    state["decoder"] = new_decoder()
                receive(queues, port, state)
                try:
                    job = queues["uart_tx_queue"].get_nowait()
                except queue.Empty:
                    job = None
                if job is not None:
                    try:
                        execute(queues, port, state, job, stop_event)
                        job["reply"].put_nowait(None)
                    except (OSError, ValueError) as exc:
                        job["reply"].put_nowait(exc)
                    finally:
                        queues["uart_tx_queue"].task_done()
                now = time.monotonic()
                if now-state["last_rx"] > cfg["heartbeat_s"] and now >= next_warning:
                    event(queues, "sensor_error", worker="stm32", message="Không nhận heartbeat STM32")
                    next_warning = now+10
            except (OSError, ValueError, serial.SerialException) as exc:
                logging.warning("UART STM32: %s", exc)
                event(queues, "sensor_error", worker="stm32", message=str(exc))
                if port is not None:
                    port.close()
                    port = None
                stop_event.wait(1)
    finally:
        if port is not None:
            try:
                write_packet(port, STOP, (state["sequence"]+1) & 0xffff)
            except OSError:
                pass
            port.close()
