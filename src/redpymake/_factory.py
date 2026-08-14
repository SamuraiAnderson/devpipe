"""顶层工厂函数 (``local`` / ``ssh`` / ``adb`` / ``serial``) 及默认 local 会话。

``rpm.stale`` 需要在使用者未显式创建 local 会话时也能将日志落到"某个会话"。
为此我们维护一个进程级的默认 ``LocalSession``：懒创建、模块级单例。
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from ._local import LocalSession

if TYPE_CHECKING:  # pragma: no cover
    from ._adb import AdbSession
    from ._serial import SerialSession
    from ._ssh import SshSession


_default_local_lock = threading.Lock()
_default_local_session: LocalSession | None = None


def _default_local() -> LocalSession:
    """获取进程级默认 local 会话（懒创建）。"""
    global _default_local_session
    if _default_local_session is None or _default_local_session.closed:
        with _default_local_lock:
            if _default_local_session is None or _default_local_session.closed:
                _default_local_session = LocalSession()
    return _default_local_session


def local(*, default_cwd: str | None = None) -> LocalSession:
    """返回一个新的 ``LocalSession``；每次调用产生独立日志缓冲。"""
    return LocalSession(default_cwd=default_cwd)


def ssh(
    host: str,
    *,
    user: str | None = None,
    port: int = 22,
    password: str | None = None,
    key_filename: str | os.PathLike[str] | None = None,
    default_cwd: str | None = None,
    connect_timeout: float = 15.0,
) -> "SshSession":
    """构造 SSH 会话（立即连接，失败抛 ``SessionConnectionError``）。"""
    from ._ssh import SshSession

    return SshSession(
        host,
        user=user,
        port=port,
        password=password,
        key_filename=key_filename,
        default_cwd=default_cwd,
        connect_timeout=connect_timeout,
    )


def adb(
    serial: str | None = None,
    *,
    adb_path: str | None = None,
    default_cwd: str | None = None,
    connect_timeout: float = 10.0,
) -> "AdbSession":
    """构造 ADB 会话（立即连接，失败抛 ``SessionConnectionError``）。"""
    from ._adb import AdbSession

    return AdbSession(
        serial=serial,
        adb_path=adb_path,
        default_cwd=default_cwd,
        connect_timeout=connect_timeout,
    )


def serial(
    port: str,
    *,
    baudrate: int = 115200,
    timeout: float = 1.0,
    newline: str = "\r\n",
    encoding: str = "utf-8",
) -> "SerialSession":
    """构造串口会话（立即打开设备，失败抛 ``SessionConnectionError``）。"""
    from ._serial import SerialSession

    return SerialSession(
        port,
        baudrate=baudrate,
        timeout=timeout,
        newline=newline,
        encoding=encoding,
    )


__all__ = ["local", "ssh", "adb", "serial"]
