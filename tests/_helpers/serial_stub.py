"""pyserial stub：捕获写出，并可从另一线程注入 RX 字节。"""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Iterator
from contextlib import contextmanager

import redpymake as rpm


class StubSerialPort:
    """模拟 ``serial.Serial``：``read`` 在无数据时短阻塞，避免读线程空转。"""

    def __init__(self, *args, **kwargs) -> None:
        self.written = bytearray()
        self.echo = False
        self._pending = bytearray()
        self._closed = False
        self._cv = threading.Condition()

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        with self._cv:
            self.written.extend(payload)
            if self.echo:
                self._pending.extend(payload)
                self._cv.notify_all()
        return len(payload)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        with self._cv:
            if not self._pending and not self._closed:
                self._cv.wait(timeout=0.05)
            if not self._pending:
                return b""
            chunk = bytes(self._pending[:n])
            del self._pending[:n]
            return chunk

    def feed(self, data: bytes) -> None:
        with self._cv:
            self._pending.extend(data)
            self._cv.notify_all()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


def install_pyserial_stub(monkeypatch) -> StubSerialPort:
    """注入 ``serial.Serial`` stub，返回同一端口实例供断言写出。"""

    fake = types.ModuleType("serial")
    port = StubSerialPort()

    def _factory(*args, **kwargs):
        return port

    fake.Serial = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial", fake)
    return port


@contextmanager
def open_stub_serial(
    monkeypatch,
    *,
    newline: str = "",
) -> Iterator[tuple[rpm.Session, StubSerialPort]]:
    """注入 pyserial stub 并打开一个串口会话。"""

    port = install_pyserial_stub(monkeypatch)
    sess = rpm.serial("COM_TEST", newline=newline)
    try:
        yield sess, port
    finally:
        sess.close()
