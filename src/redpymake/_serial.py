"""``SerialSession``：串口会话 (CORE-01)。

依赖：``pyserial``（optional extra ``[serial]``）。

串口没有严格的"命令/退出码"概念；``run()`` 将 argv 拼接成一行文本 + ``\r\n``
写入串口，并把 ``returncode`` 记为 ``0``、``stdout`` 收集"发送后一小段窗口内
读到的数据"作为便利返回值。真正常用的是 ``wait()`` 匹配日志。

不支持 ``push`` / ``pull`` / ``copy`` / ``_resource_*`` —— 全部抛
``UnsupportedOperationError``。
"""

from __future__ import annotations

import threading
import time
from typing import Mapping, Sequence

from ._path import ResourceStat
from ._session import Session
from .exceptions import (
    SessionConnectionError,
    UnsupportedOperationError,
)


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
        timeout: float = 1.0,
        newline: str = "\r\n",
        encoding: str = "utf-8",
    ) -> None:
        pyserial = _require_pyserial()
        label = f"serial:{port}@{baudrate}"
        super().__init__(session_kind="serial", session_label=label, default_cwd=None)
        self.path_style = "posix"
        try:
            self._port = pyserial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        except Exception as exc:
            raise SessionConnectionError(
                f"failed to open serial port {port}: {exc}",
                session=self,
                cause=exc,
            ) from exc
        self._newline = newline
        self._encoding = encoding
        self._stop = threading.Event()
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

    def _reader_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                data = self._port.read(256)
            except Exception:
                break
            if not data:
                # 心跳；不入日志避免刷屏
                continue
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
                self._log_buffer.append(
                    event="command_output",
                    level="INFO",
                    stream="serial",
                    message=text,
                )

    def _execute_command(
        self,
        argv: Sequence[str],
        *,
        shell: bool,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout: float | None,
        encoding: str,
        operation_id: str,
    ) -> tuple[int, str, str]:
        if env or cwd:
            # 串口无 env/cwd 概念；显式忽略而非抛错，避免误用中断脚本
            self._log_buffer.append(
                event="system",
                level="WARNING",
                stream="system",
                message="serial session ignores env/cwd on run()",
                operation_id=operation_id,
            )
        if shell:
            line = argv[0]
        else:
            line = " ".join(argv)
        payload = (line + self._newline).encode(encoding)
        cursor_before = self._log_buffer.cursor()
        try:
            self._port.write(payload)
            self._port.flush()
        except Exception as exc:
            raise SessionConnectionError(
                f"serial write failed: {exc}", session=self, cause=exc
            ) from exc

        # 收集短时间窗口内的输出作为 stdout 便利值
        window = min(0.5, timeout) if timeout is not None else 0.2
        time.sleep(window)
        recent = self._log_buffer.records(since=cursor_before, channel="serial")
        stdout = "\n".join(r.message for r in recent if r.event == "command_output")
        return 0, stdout, ""

    # ------------------------------ 资源 / 传输 ------------------------------

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

    # ------------------------------ 关闭 ------------------------------

    def _close_impl(self) -> None:
        self._stop.set()
        try:
            self._port.close()
        except Exception:
            pass
        self._reader.join(timeout=2)


__all__ = ["SerialSession"]
