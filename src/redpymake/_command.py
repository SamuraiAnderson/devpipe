"""``CommandResult`` 及 ``run().wait()`` (CORE-02 / CORE-06)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .exceptions import CommandError
from ._logs import normalize_wait_patterns

if TYPE_CHECKING:  # pragma: no cover
    from ._logs import LogCursor, LogMatch
    from ._session import Session


@dataclass(frozen=True)
class CommandResult:
    """``session.run(...)`` 的统一返回类型。

    Attributes:
        command: 实际执行的参数序列（含 argv 或 shell 命令字符串首元素）。
        returncode: 进程退出码。
        stdout: 完整标准输出文本。
        stderr: 完整标准错误文本。
        duration: 命令执行耗时（秒）。
        session: 发起命令的会话；用于 ``wait()``。
        _log_cursor_before: 会话内部保存的 run 前日志游标；``wait()`` 使用。
        _rx_cursor_before: 串口 run 前的 RX 字节偏移；字节 ``wait()`` 使用。
    """

    command: tuple[str | bytes, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    session: "Session"
    _log_cursor_before: "LogCursor | None" = field(default=None, repr=False)
    _rx_cursor_before: int | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def raise_for_status(self) -> None:
        if self.returncode != 0:
            raise CommandError(
                f"command exited with code {self.returncode}: {self.command!r}",
                session=self.session,
                command=self.command,
                returncode=self.returncode,
                stdout=self.stdout,
                stderr=self.stderr,
            )

    def wait(
        self,
        pattern: Any,
        timeout: float = 30,
        *,
        channel: str | None = None,
        multiline: bool = False,
    ) -> "LogMatch":
        """在本次 ``run()`` 之后（含执行期间）的日志 / RX 中等待模式匹配。

        通过在 ``run()`` 前保存的日志游标（或 RX 偏移）作为搜索起点，确保不会
        漏掉命令执行期间已经产生的匹配。
        """
        _pats, is_bytes = normalize_wait_patterns(pattern)
        return self.session.wait(
            pattern,
            timeout=timeout,
            channel=channel,
            since=None if is_bytes else self._log_cursor_before,
            multiline=multiline,
            command_result=self,
            rx_since=self._rx_cursor_before if is_bytes else None,
        )


__all__ = ["CommandResult"]
