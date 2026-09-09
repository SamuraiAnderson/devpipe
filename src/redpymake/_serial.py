"""``SerialSession``：串口会话 (CORE-01)。

依赖：``pyserial``（optional extra ``[serial]``）。

串口没有严格的"命令/退出码"概念；``run()`` 将文本按 encoding 编码写出
（构造参数 ``newline`` 默认空，不再自动加 ``\\r\\n``），``run(bytes)`` 原样写入。
``returncode`` 记为 ``0``；短窗口内读到的文本作为 ``stdout`` 便利值。
真正常用的是 ``wait()``：文本走日志行，字节走原始 RX 环形缓冲。

不支持 ``push`` / ``pull`` / ``copy`` / ``_resource_*`` —— 全部抛
``UnsupportedOperationError``。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping, Pattern, Sequence

from ._logs import (
    LogCursor,
    LogMatch,
    _earliest_bytes_match,
    normalize_wait_patterns,
)
from ._path import ResourceStat
from ._session import Session
from ._command import CommandResult
from .exceptions import (
    LogWaitTimeoutError,
    SessionClosedError,
    SessionConnectionError,
    UnsupportedOperationError,
)

_RX_CAPACITY = 1024 * 1024


class _RxRing:
    """容量受限的原始 RX 环形缓冲，偏移单调递增。"""

    def __init__(self, capacity: int = _RX_CAPACITY) -> None:
        self._capacity = capacity
        self._buf = bytearray()
        self._dropped = 0
        self._total = 0
        self._cond = threading.Condition()
        self._closed = False

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            if self._closed:
                return
            self._buf.extend(data)
            self._total += len(data)
            extra = len(self._buf) - self._capacity
            if extra > 0:
                del self._buf[:extra]
                self._dropped += extra
            self._cond.notify_all()

    def cursor(self) -> int:
        with self._cond:
            return self._total

    def _window_unlocked(self, start: int) -> tuple[int, bytes]:
        actual = max(start, self._dropped)
        skip = actual - self._dropped
        return actual, bytes(self._buf[skip:])


class _StreamCursor:
    """SerialSession 内部的消费进度跟踪器（文本序号 + 字节偏移），线程安全。"""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._lock = threading.Lock()
        self._rx_offset: int = 0
        self._log_seq: int = 0

    @property
    def rx_offset(self) -> int:
        with self._lock:
            return self._rx_offset

    @property
    def log_cursor(self) -> LogCursor:
        with self._lock:
            return LogCursor(self._session_id, self._log_seq)

    def advance_rx(self, end_offset: int) -> None:
        with self._lock:
            if end_offset > self._rx_offset:
                self._rx_offset = end_offset

    def advance_log(self, next_seq: int) -> None:
        with self._lock:
            if next_seq > self._log_seq:
                self._log_seq = next_seq

    def advance_to_command(self, rx_offset: int | None, log_seq: int) -> None:
        with self._lock:
            if rx_offset is not None and rx_offset > self._rx_offset:
                self._rx_offset = rx_offset
            if log_seq > self._log_seq:
                self._log_seq = log_seq


def _require_pyserial():
    try:
        import serial  # type: ignore

        return serial
    except ImportError as exc:
        raise UnsupportedOperationError(
            "serial support requires the 'pyserial' package; "
            "install with `pip install redpymake[serial]`"
        ) from exc


class SerialSession(Session):
    """基于 pyserial 的串口会话。"""

    _is_local = False

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        newline: str = "",
        encoding: str = "utf-8",
    ) -> None:
        pyserial = _require_pyserial()
        label = f"serial:{port}@{baudrate}"
        super().__init__(session_kind="serial", session_label=label, default_cwd=None)
        self.path_style = "posix"
        try:
            self._port = pyserial.Serial(port=port, baudrate=baudrate, timeout=0.05)
        except Exception as exc:
            raise SessionConnectionError(
                f"failed to open serial port {port}: {exc}",
                session=self,
                cause=exc,
            ) from exc
        self._newline = newline
        self._encoding = encoding
        self._stop = threading.Event()
        self._rx = _RxRing()
        self._cursor = _StreamCursor(self.session_id)
        self._reader = threading.Thread(
            target=self._reader_loop, name=f"serial-reader-{port}", daemon=True
        )
        self._reader.start()
        self._log_buffer.append(
            event="session_open",
            level="INFO",
            stream="system",
            message=f"serial session ready ({label})",
        )

    def _rx_cursor(self) -> int | None:
        return self._rx.cursor()

    def _reader_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                data = self._port.read(1)
                if not data:
                    if buf:
                        text = buf.decode(self._encoding, errors="replace").rstrip("\r")
                        if text:
                            self._log_buffer.append(
                                event="command_output",
                                level="INFO",
                                stream="serial",
                                message=text,
                            )
                        buf = b""
                    continue
                in_waiting = getattr(self._port, "in_waiting", 0)
                if in_waiting:
                    data += self._port.read(in_waiting)
            except Exception:
                break
            self._rx.append(data)
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(self._encoding, errors="replace").rstrip("\r")
                self._log_buffer.append(
                    event="command_output",
                    level="INFO",
                    stream="serial",
                    message=text,
                )
        if buf:
            text = buf.decode(self._encoding, errors="replace").rstrip("\r")
            if text:
                try:
                    self._log_buffer.append(
                        event="command_output",
                        level="INFO",
                        stream="serial",
                        message=text,
                    )
                except Exception:
                    pass

    def _wait_bytes(
        self,
        patterns: Sequence[Any],
        timeout: float,
        *,
        command_result: CommandResult | None,
        start_offset: int | None,
        original_pattern: Any,
    ) -> LogMatch:
        start = start_offset if start_offset is not None else self._rx.cursor()
        deadline = time.monotonic() + max(timeout, 0.0)
        t0 = time.monotonic()
        haystack = b""
        with self._rx._cond:
            while True:
                _actual, haystack = self._rx._window_unlocked(start)
                found = _earliest_bytes_match(patterns, haystack)
                if found is not None:
                    pat, index, matched, pos = found
                    abs_end = _actual + pos + len(matched)
                    return LogMatch(
                        pattern=pat,
                        record=None,
                        text=matched,
                        elapsed=time.monotonic() - t0,
                        index=index,
                        command_result=command_result,
                        session=self,
                        _rx_end=abs_end,
                    )
                if self._rx._closed or self.closed:
                    raise SessionClosedError(
                        f"session '{self.session_id}' closed while waiting for pattern",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._rx._cond.wait(timeout=remaining)

        raise LogWaitTimeoutError(
            f"timeout after {timeout}s waiting for pattern {original_pattern!r}",
            pattern=original_pattern,
            timeout=timeout,
            records=(),
            output="",
            command_result=command_result,
            data=haystack,
        )

    def wait(
        self,
        pattern: str | bytes | Pattern[str] | Pattern[bytes] | Sequence[Any],
        timeout: float = 30,
        *,
        channel: str | None = None,
        since: LogCursor | None = None,
        command_result: CommandResult | None = None,
        multiline: bool = False,
        rx_since: int | None = None,
    ) -> LogMatch:
        self._check_alive()
        root = getattr(self, "root", self)
        cursor: _StreamCursor | None = getattr(root, "_cursor", None)
        _pats, is_bytes = normalize_wait_patterns(pattern)
        if is_bytes:
            start_off = (
                rx_since
                if rx_since is not None
                else (cursor.rx_offset if cursor is not None else None)
            )
            match = super().wait(
                pattern,
                timeout=timeout,
                channel=channel,
                since=since,
                command_result=command_result,
                multiline=multiline,
                rx_since=start_off,
            )
            if cursor is not None and match._rx_end is not None:
                cursor.advance_rx(match._rx_end)
            return match
        else:
            effective_since = (
                since
                if since is not None
                else (cursor.log_cursor if cursor is not None else None)
            )
            match = super().wait(
                pattern,
                timeout=timeout,
                channel=channel,
                since=effective_since,
                command_result=command_result,
                multiline=multiline,
                rx_since=rx_since,
            )
            if cursor is not None and match.record is not None:
                cursor.advance_log(match.record.sequence + 1)
            return match

    def _execute_command(
        self,
        argv: Sequence[str | bytes],
        *,
        shell: bool,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout: float | None,
        encoding: str,
        operation_id: str,
    ) -> tuple[int, str, str]:
        if env or cwd:
            self._log_buffer.append(
                event="system",
                level="WARNING",
                stream="system",
                message="serial session ignores env/cwd on run()",
                operation_id=operation_id,
            )
        if len(argv) == 1 and isinstance(argv[0], bytes):
            payload = argv[0]
        else:
            if not all(isinstance(a, str) for a in argv):
                raise TypeError("serial text run() requires str arguments")
            line = argv[0] if shell else " ".join(argv)  # type: ignore[arg-type]
            payload = line.encode(encoding) + self._newline.encode(encoding)
        cursor_before = self._log_buffer.cursor()
        rx_before = self._rx_cursor()
        self._cursor.advance_to_command(rx_before, cursor_before.sequence)
        try:
            self._port.write(payload)
            self._port.flush()
        except Exception as exc:
            raise SessionConnectionError(
                f"serial write failed: {exc}", session=self, cause=exc
            ) from exc

        window = min(0.5, timeout) if timeout is not None else 0.2
        time.sleep(window)
        recent = self._log_buffer.records(since=cursor_before, channel="serial")
        stdout = "\n".join(r.message for r in recent if r.event == "command_output")
        return 0, stdout, ""

    def _resource_exists(self, path: str) -> bool:
        raise UnsupportedOperationError("serial sessions do not expose filesystem")

    def _resource_is_file(self, path: str) -> bool:
        raise UnsupportedOperationError("serial sessions do not expose filesystem")

    def _resource_is_dir(self, path: str) -> bool:
        raise UnsupportedOperationError("serial sessions do not expose filesystem")

    def _resource_stat(self, path: str) -> ResourceStat:
        raise UnsupportedOperationError("serial sessions do not expose filesystem")

    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None:
        raise UnsupportedOperationError("serial sessions do not expose filesystem")

    def _resource_mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        raise UnsupportedOperationError("serial sessions do not expose filesystem")

    def _close_impl(self) -> None:
        self._stop.set()
        self._rx.close()
        try:
            if hasattr(self._port, "cancel_read"):
                self._port.cancel_read()
        except Exception:
            pass
        try:
            self._port.close()
        except Exception:
            pass
        self._reader.join(timeout=2)


__all__ = ["SerialSession"]
