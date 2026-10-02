"""Standalone, versioned UART bridge. Shipped as source; requires only pyserial.

No RedPyMake imports: this file is passed to the remote interpreter in memory.
stdout is exclusively framed binary protocol; stderr is diagnostic text.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import threading
import time

_HEADER = struct.Struct("!4sBII")
_MAGIC = b"RPM1"
_MAX_PAYLOAD = 65536
_READY, _DATA, _IDLE, _TX, _ACK, _ERROR, _CLOSE, _CLOSED, _ALIVE = range(1, 10)


def _main() -> int:
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    source, destination = sys.stdin.buffer.raw, sys.stdout.buffer
    stop = threading.Event()
    write_lock = threading.Lock()
    sequence = 0
    failures = []
    last_control = time.monotonic()

    def send(kind, payload=b""):
        nonlocal sequence
        with write_lock:
            sequence += 1
            frame = _HEADER.pack(_MAGIC, kind, sequence, len(payload)) + payload
            view = memoryview(frame)
            while view:
                written = destination.write(view)
                if not written:
                    raise OSError("bridge stdout closed")
                view = view[written:]
            destination.flush()

    def fail(exc):
        failures.append(exc)
        stop.set()
        try:
            send(_ERROR, (type(exc).__name__ + ": " + str(exc)).encode("utf-8")[:_MAX_PAYLOAD])
        except (OSError, ValueError) as broken:
            sys.stderr.write(f"UART bridge failure: {exc}; reporting failed: {broken}\n")

    try:
        config = json.loads(base64.b64decode(sys.argv[1]))
        import serial
        port = serial.Serial(port=config["port"], baudrate=config["baudrate"], timeout=.05,
                             write_timeout=config["write_timeout"], xonxoff=False, rtscts=False)
    except Exception as exc:
        fail(exc)
        return 1

    def receive_exact(count):
        chunks = bytearray()
        while len(chunks) < count:
            data = source.read(count - len(chunks))
            if not data:
                if chunks:
                    raise OSError("truncated UART control frame")
                return None
            chunks.extend(data)
        return bytes(chunks)

    def controls():
        nonlocal last_control
        expected = 1
        try:
            while not stop.is_set():
                header = receive_exact(_HEADER.size)
                if header is None:
                    stop.set()
                    return
                magic, kind, request_id, length = _HEADER.unpack(header)
                if magic != _MAGIC or request_id != expected or length > _MAX_PAYLOAD:
                    raise ValueError("invalid UART control frame header")
                expected += 1
                payload = receive_exact(length) if length else b""
                if payload is None:
                    raise OSError("truncated UART control payload")
                last_control = time.monotonic()
                if kind == _ALIVE and not payload:
                    continue
                if kind == _CLOSE and not payload:
                    stop.set()
                    return
                if kind != _TX:
                    raise ValueError("unsupported UART control frame")
                offset = 0
                while offset < length:
                    written = port.write(payload[offset:])
                    if not written:
                        raise OSError("UART write made no progress")
                    offset += written
                # write_timeout bounds write; ACK deliberately promises accepted bytes,
                # not physical transmission or device command completion.
                send(_ACK, struct.pack("!II", request_id, offset))
        except Exception as exc:
            fail(exc)

    def reader():
        pending = False
        heartbeat = time.monotonic() + 1
        try:
            while not stop.is_set():
                data = port.read(1)
                if data:
                    waiting = min(getattr(port, "in_waiting", 0), 32767)
                    if waiting:
                        data += port.read(waiting)
                    send(_DATA, data)
                    pending = True
                elif pending:
                    send(_IDLE)
                    pending = False
                if time.monotonic() >= heartbeat:
                    send(_ALIVE)
                    heartbeat = time.monotonic() + 1
        except Exception as exc:
            if not stop.is_set():
                fail(exc)

    try:
        send(_READY, b"1")
        input_thread = threading.Thread(target=controls, daemon=True)
        read_thread = threading.Thread(target=reader, daemon=True)
        input_thread.start()
        read_thread.start()
        while not stop.wait(.2):
            if time.monotonic() - last_control > config["lease"]:
                failures.append(OSError("UART client lease expired"))
                stop.set()
        read_thread.join(timeout=1)
    except Exception as exc:
        fail(exc)
    finally:
        try:
            port.close()
            def finish():
                try:
                    if failures:
                        send(_ERROR, str(failures[0]).encode("utf-8")[:_MAX_PAYLOAD])
                    send(_CLOSED)
                except Exception as exc:
                    failures.append(exc)
                    sys.stderr.write(f"UART final frame failed: {exc}\n")
            finalizer = threading.Thread(target=finish, daemon=True)
            finalizer.start()
            finalizer.join(timeout=1)
            if finalizer.is_alive():
                failures.append(OSError("UART final frame blocked"))
        except Exception as exc:
            failures.append(exc)
            sys.stderr.write(f"UART cleanup failed: {exc}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    # All writes are flushed explicitly. Do not wait on a blocked daemon pipe
    # reader during interpreter shutdown after the client's lease expires.
    os._exit(_main())
