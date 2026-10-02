"""Client side of the binary UART bridge, separate from text log parsing."""
from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ._process import _Deadline
from ._serial_bridge import (_HEADER, _MAGIC, _MAX_PAYLOAD, _READY, _DATA, _IDLE,
                             _TX, _ACK, _ERROR, _CLOSE, _CLOSED, _ALIVE)
from .exceptions import SessionClosedError, SessionConnectionError, CommandTimeoutError


class _RemoteSerialPort:
    def __init__(self, host: Any, session: Any, *, port: str, baudrate: int,
                 python_path: str | None, connect_timeout: float) -> None:
        self.host, self.session = host, session
        self.timeout = connect_timeout
        self._closed = False
        self._closing = False
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._events: deque[tuple[str, bytes]] = deque()
        self._queued_bytes = 0
        self._failure: Exception | None = None
        self._ready = threading.Event()
        self._closed_ack = threading.Event()
        self._acks: dict[int, int] = {}
        self._request = 0
        self._write_lock = threading.Lock()
        self._diagnostic = bytearray()
        self._ready_received = False
        deadline = _Deadline.start(connect_timeout)
        executable = python_path or host.executable("python" if host.platform == "windows" else "python3",
                                                   timeout=deadline.remaining())
        source = Path(__file__).with_name("_serial_bridge.py").read_bytes()
        config = base64.b64encode(json.dumps({"port": port, "baudrate": baudrate,
            "write_timeout": connect_timeout, "lease": max(5, 2 * connect_timeout)}).encode()).decode("ascii")
        # Stream the payload: embedding it in -c exceeds Windows SSH command limits.
        code = ("import os,sys;"
                "exec('if os.name == \"nt\":\\n import msvcrt;msvcrt.setmode(0,os.O_BINARY)');"
                f"exec(compile(sys.stdin.buffer.read({len(source)}),'<rpm-uart>','exec'))")
        self._process = host.spawn([executable, "-u", "-c", code, config], timeout=deadline.remaining())
        self._threads = [threading.Thread(target=self._receive, daemon=True, name="rpm-uart-rx"),
                         threading.Thread(target=self._stderr, daemon=True, name="rpm-uart-stderr")]
        for thread in self._threads:
            thread.start()
        try:
            self._process.write(source, deadline)
            if not self._ready.wait(deadline.remaining()):
                raise SessionConnectionError("UART bridge READY timed out", session=session)
            self._check()
            heartbeat = threading.Thread(target=self._heartbeat, daemon=True, name="rpm-uart-tx-heartbeat")
            self._threads.append(heartbeat)
            heartbeat.start()
        except BaseException as exc:
            try:
                self.close()
            except Exception as cleanup:
                setattr(exc, "cleanup_error", cleanup)
            raise

    def _check(self) -> None:
        if self._failure is not None:
            raise SessionConnectionError(f"UART bridge: {self._failure}", session=self.session,
                                         cause=self._failure, outcome="unknown") from self._failure
        if self._closed:
            raise SessionClosedError("UART bridge is closed", session=self.session)
        self.host.session._check_alive()

    def _heartbeat(self) -> None:
        while not self._stop.wait(1):
            if not self._write_lock.acquire(timeout=.1):
                continue  # TX itself renews the remote lease.
            try:
                if self._closing:
                    return
                self._check()
                self._request += 1
                self._process.write(_HEADER.pack(_MAGIC, _ALIVE, self._request, 0), _Deadline.start(2))
            except Exception as exc:
                if not self._closing:
                    self._failed(exc)
                return
            finally:
                self._write_lock.release()

    def _failed(self, exc: Exception) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = exc
            self._condition.notify_all()
        self._ready.set()
        if not self._closing:
            self.session._fail(exc)

    def _stderr(self) -> None:
        try:
            while not self._closed:
                try:
                    data = self._process.read(4096, stderr=True)
                except socket.timeout:
                    continue
                if not data:
                    return
                self._diagnostic.extend(data)
                if len(self._diagnostic) > 16384:
                    del self._diagnostic[:-16384]
        except Exception as exc:
            if not self._closing:
                self._failed(exc)

    def _receive(self) -> None:
        pending = bytearray()
        expected = 1
        last_frame = time.monotonic()
        try:
            while not self._closed:
                self.host.session._check_alive()
                try:
                    data = self._process.read(32768)
                except socket.timeout:
                    if time.monotonic() - last_frame > max(5, self.timeout):
                        raise OSError("UART bridge heartbeat lost")
                    continue
                if not data:
                    if self._closed_ack.is_set():
                        return
                    detail = self._diagnostic.decode("utf-8", errors="replace")
                    raise OSError("UART bridge ended unexpectedly: " + detail)
                pending.extend(data)
                while len(pending) >= _HEADER.size:
                    magic, kind, sequence, length = _HEADER.unpack(pending[:_HEADER.size])
                    if magic != _MAGIC or sequence != expected or length > _MAX_PAYLOAD:
                        raise OSError("invalid UART bridge frame header/version/sequence")
                    if len(pending) < _HEADER.size + length:
                        break
                    payload = bytes(pending[_HEADER.size:_HEADER.size + length])
                    del pending[:_HEADER.size + length]
                    expected += 1
                    last_frame = time.monotonic()
                    if not self._ready_received and kind not in {_READY, _ERROR}:
                        raise OSError("UART bridge data before READY")
                    with self._condition:
                        if kind == _READY and payload == b"1" and not self._ready_received:
                            self._ready_received = True
                            self._ready.set()
                        elif kind in {_DATA, _IDLE}:
                            if kind == _IDLE and payload:
                                raise OSError("invalid UART idle event")
                            self._events.append(("data" if kind == _DATA else "idle", payload))
                            self._queued_bytes += len(payload)
                            if self._queued_bytes > 1024 * 1024 or len(self._events) > 16384:
                                raise OSError("UART receive queue overflow")
                        elif kind == _ACK and len(payload) == 8:
                            request, count = struct.unpack("!II", payload)
                            if request != self._request or request in self._acks:
                                raise OSError("unexpected UART write acknowledgement")
                            self._acks[request] = count
                        elif kind == _ERROR:
                            raise OSError(payload.decode("utf-8", errors="replace"))
                        elif kind == _CLOSED and not payload:
                            self._closed_ack.set()
                            if not self._closing:
                                raise OSError("UART bridge closed unexpectedly")
                        elif kind == _ALIVE and not payload:
                            pass  # Explicit protocol heartbeat; no UART idle semantics.
                        else:
                            raise OSError("invalid UART bridge frame type/payload")
                        self._condition.notify_all()
        except Exception as exc:
            if not self._closing:
                self._failed(exc)

    def read_event(self) -> tuple[str, bytes]:
        with self._condition:
            self._check()
            if not self._events:
                self._condition.wait(timeout=.2)
                self._check()
            if not self._events:
                return "tick", b""
            kind, payload = self._events.popleft()
            self._queued_bytes -= len(payload)
            return kind, payload

    def write(self, data: bytes, *, timeout: float | None = None) -> int:
        deadline = _Deadline.start(self.timeout if timeout is None else timeout)
        with self._write_lock:
            self._check()
            offset = 0
            try:
                while offset < len(data):
                    chunk = data[offset:offset + _MAX_PAYLOAD]
                    self._request += 1
                    request = self._request
                    self._process.write(_HEADER.pack(_MAGIC, _TX, request, len(chunk)) + chunk, deadline)
                    with self._condition:
                        while request not in self._acks:
                            self._check()
                            remaining = deadline.remaining()
                            self._condition.wait(timeout=min(.2, remaining) if remaining is not None else .2)
                        count = self._acks.pop(request)
                    if count != len(chunk):
                        raise OSError("incomplete UART write acknowledgement")
                    offset += count
            except Exception as exc:
                self._failed(exc)
                if isinstance(exc, CommandTimeoutError):
                    raise CommandTimeoutError("UART write timed out; delivery is unconfirmed",
                                              session=self.session, timeout=timeout) from exc
                raise SessionConnectionError(f"UART write unconfirmed: {exc}", session=self.session,
                                             cause=exc, outcome="unknown") from exc
            return offset

    def flush(self) -> None:
        self._check()  # ACK confirms acceptance by the serial driver, not device execution.

    def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        self._stop.set()
        failure = None
        if self._failure is None and self.host.session._failure is None and not self.host.session.closed:
            try:
                with self._write_lock:
                    self._request += 1
                    self._process.write(_HEADER.pack(_MAGIC, _CLOSE, self._request, 0), _Deadline.start(2))
                if not self._closed_ack.wait(2):
                    raise OSError("UART bridge cleanup unconfirmed")
            except Exception as exc:
                failure = exc
        self._closed = True
        with self._condition:
            self._condition.notify_all()
        self._process.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        if failure:
            raise SessionConnectionError(str(failure), session=self.session, cause=failure) from failure
