"""顶层工厂函数 (``local`` / ``ssh`` / ``adb`` / ``serial`` / ``wsl``) 及默认 local 会话。

``rpm.stale`` 需要在使用者未显式创建 local 会话时也能将日志落到"某个会话"。
为此我们维护一个进程级的默认 ``LocalSession``：懒创建、模块级单例。

**CORE-11 联动**：当前调用在 ``rpm.workspace(...)`` 作用域内时（``_active_workspace``
ContextVar 非空），顶层工厂**改为向 Workspace 借出**共享会话（返回
``_BorrowedSession`` 代理，`__exit__` 不 close 真会话）；否则维持"每次调用独立会话"
的原语义（等价 CLI 直跑）。
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from ._local import LocalSession

if TYPE_CHECKING:  # pragma: no cover
    from ._adb import AdbSession
    from ._serial import SerialSession
    from ._session import Session
    from ._ssh import SshSession
    from ._wsl import WslSession


_default_local_lock = threading.Lock()
_default_local_session: LocalSession | None = None


def _default_local() -> LocalSession:
    """获取进程级默认 local 会话（懒创建）。

    仅用于库内部（``rpm.stale`` / 传输中转 / ``_as_path``），不受 workspace 借出
    机制影响，保证语义稳定。
    """
    global _default_local_session
    if _default_local_session is None or _default_local_session.closed:
        with _default_local_lock:
            if _default_local_session is None or _default_local_session.closed:
                _default_local_session = LocalSession()
    return _default_local_session


def _active_workspace_or_none():
    """惰性读取当前活跃 workspace，避免 ``_workspace`` 的循环导入。"""
    try:
        from ._workspace import _active_workspace
    except ImportError:  # pragma: no cover - 循环导入极端情况
        return None
    ws = _active_workspace.get()
    if ws is None or getattr(ws, "closed", False):
        return None
    return ws


def local(*, default_cwd: str | None = None) -> "Session":
    """返回一个 ``LocalSession``（或 workspace 借出的代理）。

    - workspace 作用域外：每次调用产生**独立**会话（日志缓冲不共享）；
    - workspace 作用域内：从池中借出共享会话，出 ``with`` 块不 close。
    """
    ws = _active_workspace_or_none()
    if ws is not None:
        return ws.local(default_cwd=default_cwd)
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
) -> "Session":
    """构造 SSH 会话（立即连接，失败抛 ``SessionConnectionError``）。"""
    ws = _active_workspace_or_none()
    if ws is not None:
        return ws.ssh(
            host,
            user=user,
            port=port,
            password=password,
            key_filename=key_filename,
            default_cwd=default_cwd,
            connect_timeout=connect_timeout,
        )
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
) -> "Session":
    """构造 ADB 会话（立即连接，失败抛 ``SessionConnectionError``）。"""
    ws = _active_workspace_or_none()
    if ws is not None:
        return ws.adb(
            serial=serial,
            adb_path=adb_path,
            default_cwd=default_cwd,
            connect_timeout=connect_timeout,
        )
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
) -> "Session":
    """构造串口会话（立即打开设备，失败抛 ``SessionConnectionError``）。"""
    ws = _active_workspace_or_none()
    if ws is not None:
        return ws.serial(
            port,
            baudrate=baudrate,
            timeout=timeout,
            newline=newline,
            encoding=encoding,
        )
    from ._serial import SerialSession

    return SerialSession(
        port,
        baudrate=baudrate,
        timeout=timeout,
        newline=newline,
        encoding=encoding,
    )


def wsl(
    distribution: str | None = None,
    *,
    user: str | None = None,
    wsl_path: str | None = None,
    default_cwd: str | None = None,
) -> "Session":
    """构造 WSL 会话。

    与 SSH/ADB 不同，此工厂**只校验 ``wsl.exe`` 是否可执行**（不做 distro 级
    探测），是 CORE-01 "构造时立即连接"要求的显式例外；distro 未安装 / 冷启动
    失败等情形延迟到首次 ``run()`` 时以 ``CommandError`` 呈现。``wsl.exe``
    不存在则抛 ``SessionConnectionError``。
    """
    ws = _active_workspace_or_none()
    if ws is not None:
        return ws.wsl(
            distribution=distribution,
            user=user,
            wsl_path=wsl_path,
            default_cwd=default_cwd,
        )
    from ._wsl import WslSession

    return WslSession(
        distribution=distribution,
        user=user,
        wsl_path=wsl_path,
        default_cwd=default_cwd,
    )


__all__ = ["local", "ssh", "adb", "serial", "wsl"]
