"""RedPyMake 核心库公开 API。

推荐用法::

    import redpymake as rpm

    with rpm.ssh("192.168.1.10", user="root") as remote:
        workspace = remote.at("/workspace")
        workspace.run("cmake", "-B", "build")
        if rpm.stale(workspace.path("build/app"), depends_on=workspace.path("src/main.c")):
            workspace.run("cmake", "--build", "build", "-j8")

顶层导出的名字由 ``__all__`` 明确定义；``_*.py`` 是实现细节，不保证稳定。
"""

from __future__ import annotations

from ._command import CommandResult
from ._factory import adb, local, serial, ssh
from ._local import LocalSession
from ._logs import (
    LogBuffer,
    LogCursor,
    LogMatch,
    SessionLogRecord,
    SessionLogs,
)
from ._path import ResourcePath, ResourceStat
from ._session import Session
from ._stale import PathSpec, StalePredicate, stale
from ._transfer import TransferResult
from .exceptions import (
    CommandError,
    CommandTimeoutError,
    InputNotFoundError,
    LogWaitTimeoutError,
    RedPyMakeError,
    ResourceError,
    ResourceNotFoundError,
    SessionClosedError,
    SessionConnectionError,
    SessionError,
    TransferError,
    UnsupportedOperationError,
)

__version__ = "0.5.0"


def __getattr__(name: str):
    """按需暴露可选平台会话类型（避免强制导入 paramiko/pyserial）。"""
    if name == "SshSession":
        from ._ssh import SshSession as _SshSession

        return _SshSession
    if name == "AdbSession":
        from ._adb import AdbSession as _AdbSession

        return _AdbSession
    if name == "SerialSession":
        from ._serial import SerialSession as _SerialSession

        return _SerialSession
    raise AttributeError(f"module 'redpymake' has no attribute {name!r}")


__all__ = [
    "__version__",
    # 工厂
    "local",
    "ssh",
    "adb",
    "serial",
    # 一级类型
    "Session",
    "LocalSession",
    "SshSession",
    "AdbSession",
    "SerialSession",
    # 数据对象
    "CommandResult",
    "TransferResult",
    "ResourcePath",
    "ResourceStat",
    # 日志
    "SessionLogs",
    "SessionLogRecord",
    "LogBuffer",
    "LogCursor",
    "LogMatch",
    # 过时判断
    "stale",
    "StalePredicate",
    "PathSpec",
    # 异常
    "RedPyMakeError",
    "SessionError",
    "SessionConnectionError",
    "SessionClosedError",
    "CommandError",
    "CommandTimeoutError",
    "TransferError",
    "ResourceError",
    "ResourceNotFoundError",
    "InputNotFoundError",
    "LogWaitTimeoutError",
    "UnsupportedOperationError",
]
