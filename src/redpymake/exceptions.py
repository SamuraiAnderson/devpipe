"""RedPyMake 统一异常层次 (CORE-07)。

所有对外抛出的错误都继承自 ``RedPyMakeError``，避免混杂 Paramiko / RuntimeError
等第三方或通用异常泄漏到调用者。异常上尽量保留可程序化访问的字段（会话、命令、
超时、源目标等），方便测试与日志记录。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from ._logs import SessionLogRecord
    from ._path import ResourcePath
    from ._session import Session


class RedPyMakeError(Exception):
    """所有 RedPyMake 抛出的异常基类。"""


class SessionError(RedPyMakeError):
    """会话相关错误的公共基类。"""

    def __init__(self, message: str, *, session: "Session | None" = None) -> None:
        super().__init__(message)
        self.session = session


class SessionConnectionError(SessionError):
    """构造会话或后续通信中连接失败。"""

    def __init__(
        self,
        message: str,
        *,
        session: "Session | None" = None,
        host: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, session=session)
        self.host = host
        self.cause = cause


class SessionClosedError(SessionError):
    """在已经关闭的会话上继续调用接口。"""


class CommandError(RedPyMakeError):
    """命令以非零退出码结束（默认 ``check=True``）。"""

    def __init__(
        self,
        message: str,
        *,
        session: "Session | None" = None,
        command: Sequence[str] | None = None,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.session = session
        self.command: tuple[str, ...] = tuple(command) if command else ()
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CommandTimeoutError(CommandError):
    """命令执行超时。"""

    def __init__(
        self,
        message: str,
        *,
        session: "Session | None" = None,
        command: Sequence[str] | None = None,
        timeout: float | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(
            message,
            session=session,
            command=command,
            returncode=None,
            stdout=stdout,
            stderr=stderr,
        )
        self.timeout = timeout


class TransferError(RedPyMakeError):
    """push/pull/copy 传输失败。"""

    def __init__(
        self,
        message: str,
        *,
        source: Any = None,
        target: Any = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.target = target
        self.cause = cause


class ResourceError(RedPyMakeError):
    """资源（路径/文件）访问相关错误的公共基类。"""

    def __init__(self, message: str, *, path: Any = None) -> None:
        super().__init__(message)
        self.path = path


class ResourceNotFoundError(ResourceError):
    """访问一个不存在的资源。"""


class InputNotFoundError(ResourceError):
    """``rpm.stale`` 求值时依赖不存在；始终抛出，不当作 True。"""

    def __init__(
        self,
        message: str,
        *,
        path: Any = None,
        name: str | None = None,
    ) -> None:
        super().__init__(message, path=path)
        self.name = name


class LogWaitTimeoutError(RedPyMakeError):
    """``session.wait`` / ``CommandResult.wait`` 在超时时间内未匹配到目标模式。"""

    def __init__(
        self,
        message: str,
        *,
        pattern: Any = None,
        timeout: float | None = None,
        records: "Iterable[SessionLogRecord] | None" = None,
        output: str = "",
        command_result: Any = None,
        data: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.pattern = pattern
        self.timeout = timeout
        self.records = tuple(records) if records is not None else ()
        self.output = output
        self.command_result = command_result
        self.data = data


class UnsupportedOperationError(RedPyMakeError):
    """当前平台不支持所请求的能力。用于替代直接暴露 NotImplementedError。"""


class WorkspaceStoppedError(RedPyMakeError):
    """用户通过 ``Workspace.stop_current()`` 请求终止当前 run。

    协作式取消：借出会话在每次 ``run()`` / ``wait()`` 进入前检查停止标志，已置起
    则抛出本异常，让脚本在**下一条命令的边界**退出。已经跑起来的子进程不强杀。
    """

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


__all__ = [
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
    "WorkspaceStoppedError",
]
