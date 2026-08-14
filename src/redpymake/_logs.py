"""会话日志收集、订阅、tag 与等待 (CORE-06)。

本模块提供：

- ``SessionLogRecord``：结构化日志记录。
- ``LogBuffer``：线程安全的容量受限缓冲，用作单个会话的收集器。
- ``LogCursor``：日志游标，用于 ``run().wait()`` 时区分 ``run`` 前后的日志。
- ``LogMatch``：等待成功后返回的匹配结果。
- ``SessionLogs``：暴露给用户的日志入口对象（``session.logs``）。

线程模型：
    ``LogBuffer.append`` 与 ``SessionLogs.wait`` 在多线程/协程环境下要求安全。
    为此使用 ``threading.Lock`` + ``threading.Condition`` 组合：写入端持锁追加
    记录并通知条件变量；等待端在同一锁下先扫描既有记录，未命中时进入 wait。
    这样保证了 ``run().wait(...)`` 不会漏掉 ``run`` 执行期间已经产生的匹配。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Pattern,
    Sequence,
)

from .exceptions import LogWaitTimeoutError, SessionClosedError

if TYPE_CHECKING:  # pragma: no cover
    from ._command import CommandResult


# 库不得调用 logging.basicConfig()；此 logger 供内部诊断使用，
# 由调用者自行配置 handler。
_diag_logger = logging.getLogger("redpymake")


@dataclass(frozen=True)
class SessionLogRecord:
    """单条会话日志记录。

    Attributes:
        timestamp: Unix 时间戳（秒，float）。
        sequence: 会话内递增的序号，用作 ``LogCursor`` 的基础。
        session_id: 所属会话标识。
        event: 事件类型（``connect`` / ``command_start`` / ``command_output`` /
            ``command_end`` / ``transfer_start`` / ``transfer_end`` /
            ``stale.check`` / ``system`` / ``error`` 等）。
        level: 日志级别（``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``）。
        stream: 数据来源（``stdout`` / ``stderr`` / ``serial`` / ``system``）。
        message: 文本消息（对 stdout/stderr 就是一行输出，剔除末尾换行）。
        operation_id: 若属于某个命令/传输，携带其操作 id；否则为 ``None``。
        fields: 结构化字段（含 ``logs.tag(**kwargs)`` 注入的标签）。
    """

    timestamp: float
    sequence: int
    session_id: str
    event: str
    level: str
    stream: str
    message: str
    operation_id: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LogCursor:
    """日志游标：表示"下一条待观察记录的序号"。"""

    session_id: str
    sequence: int


@dataclass(frozen=True)
class LogMatch:
    """``wait()`` 成功返回的匹配结果。"""

    pattern: str | Pattern[str]
    record: SessionLogRecord
    text: str
    elapsed: float
    command_result: "CommandResult | None" = None


Subscriber = Callable[[SessionLogRecord], None]


class LogBuffer:
    """线程安全、容量受限的会话日志缓冲。

    保留最近 ``capacity`` 条记录；序号 (``sequence``) 单调递增，不因裁剪而重置。
    """

    __slots__ = (
        "_session_id",
        "_capacity",
        "_records",
        "_lock",
        "_cond",
        "_seq",
        "_subscribers",
        "_closed",
    )

    def __init__(self, session_id: str, *, capacity: int = 10_000) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._session_id = session_id
        self._capacity = capacity
        self._records: deque[SessionLogRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = count(0)
        self._subscribers: list[Subscriber] = []
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def next_sequence(self) -> int:
        """预取下一条记录的序号，用于构造 ``LogCursor``。

        注意：与 ``append`` 使用同一 ``count``，不同调用可能穿插；这里只在
        锁保护下使用，用于原子地获取当前 cursor。
        """
        with self._lock:
            # 通过窥视 deque 尾部得到"下一条会写入的序号"。
            if self._records:
                return self._records[-1].sequence + 1
            return 0

    def cursor(self) -> LogCursor:
        return LogCursor(session_id=self._session_id, sequence=self.next_sequence())

    def append(
        self,
        *,
        event: str,
        level: str,
        stream: str,
        message: str,
        operation_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> SessionLogRecord:
        merged_fields = dict(_ambient_tags.get() or ())
        if fields:
            merged_fields.update(fields)
        with self._cond:
            if self._closed:
                # 关闭后仍允许读取历史，但拒绝新写入以避免歧义。
                raise SessionClosedError(
                    f"log buffer for session '{self._session_id}' is closed",
                )
            seq = next(self._seq)
            record = SessionLogRecord(
                timestamp=time.time(),
                sequence=seq,
                session_id=self._session_id,
                event=event,
                level=level,
                stream=stream,
                message=message,
                operation_id=operation_id,
                fields=merged_fields,
            )
            self._records.append(record)
            self._cond.notify_all()
            subscribers = list(self._subscribers)

        # 通知订阅者放在锁外，避免用户回调阻塞 append。
        for sub in subscribers:
            try:
                sub(record)
            except Exception:  # pragma: no cover - 用户回调不应影响主流程
                _diag_logger.exception("log subscriber raised")

        return record

    def close(self) -> None:
        """标记缓冲已关闭；已有记录仍可读取和保存。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed

    def records(
        self,
        *,
        since: LogCursor | None = None,
        channel: str | None = None,
    ) -> list[SessionLogRecord]:
        with self._lock:
            snapshot = list(self._records)
        start_seq = 0
        if since is not None:
            if since.session_id != self._session_id:
                raise ValueError("cursor belongs to a different session")
            start_seq = since.sequence
        result: list[SessionLogRecord] = []
        for rec in snapshot:
            if rec.sequence < start_seq:
                continue
            if channel is not None and rec.stream != channel:
                continue
            result.append(rec)
        return result

    def text(
        self,
        *,
        since: LogCursor | None = None,
        channel: str | None = None,
        include_events: Iterable[str] | None = ("command_output",),
    ) -> str:
        """拼接指定范围/来源的消息文本。

        默认只拼接命令输出行（``command_output``），便于日志保存与 wait 后回溯。
        传入 ``include_events=None`` 表示拼接所有事件。
        """
        include: set[str] | None = set(include_events) if include_events is not None else None
        lines: list[str] = []
        for rec in self.records(since=since, channel=channel):
            if include is not None and rec.event not in include:
                continue
            lines.append(rec.message)
        return "\n".join(lines)

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """注册实时订阅者，返回取消订阅函数。"""

        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    # ---------- wait 相关 ----------

    def wait(
        self,
        pattern: str | Pattern[str],
        timeout: float,
        *,
        channel: str | None = None,
        since: LogCursor | None = None,
        command_result: "CommandResult | None" = None,
    ) -> LogMatch:
        """阻塞等待模式匹配某条日志记录。

        - 先扫描 ``since`` 之后的既有记录，再等待新写入的记录。
        - 支持字符串（子串匹配）与 ``re.Pattern``（正则搜索）。
        - 超时抛 ``LogWaitTimeoutError``；会话关闭抛 ``SessionClosedError``。
        """

        matcher = _compile_matcher(pattern)
        deadline = time.monotonic() + max(timeout, 0.0)
        start = time.monotonic()

        # 起始位置：若未指定则从下一条开始，避免匹配到 wait 调用之前的全部历史。
        start_seq = since.sequence if since is not None else self.next_sequence()

        # channel 语义：显式指定则严格过滤；未指定时只匹配"数据日志"
        # （stdout/stderr/serial），不匹配 system 框架事件，避免 wait 意外
        # 匹配到 ``$ command_line`` 之类的记录。
        allowed_streams: set[str] | None
        if channel is not None:
            allowed_streams = {channel}
        else:
            allowed_streams = {"stdout", "stderr", "serial"}

        with self._cond:
            if since is not None and since.session_id != self._session_id:
                raise ValueError("cursor belongs to a different session")

            scan_from_idx = 0
            while True:
                # 从缓冲中查找 sequence >= start_seq 且匹配的记录。
                # deque 是按追加顺序的，从头到尾即可。
                for idx in range(scan_from_idx, len(self._records)):
                    rec = self._records[idx]
                    if rec.sequence < start_seq:
                        continue
                    if allowed_streams is not None and rec.stream not in allowed_streams:
                        continue
                    if matcher(rec.message):
                        elapsed = time.monotonic() - start
                        return LogMatch(
                            pattern=pattern,
                            record=rec,
                            text=rec.message,
                            elapsed=elapsed,
                            command_result=command_result,
                        )
                scan_from_idx = len(self._records)

                if self._closed:
                    raise SessionClosedError(
                        f"session '{self._session_id}' closed while waiting for pattern",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)

        # 超时：收集 since 之后的记录与文本供异常携带
        records = self.records(since=since, channel=channel)
        output = "\n".join(r.message for r in records if r.event == "command_output")
        raise LogWaitTimeoutError(
            f"timeout after {timeout}s waiting for pattern {pattern!r}",
            pattern=pattern,
            timeout=timeout,
            records=records,
            output=output,
            command_result=command_result,
        )


def _compile_matcher(pattern: str | Pattern[str]) -> Callable[[str], bool]:
    if isinstance(pattern, str):
        needle = pattern
        return lambda text: needle in text
    if isinstance(pattern, re.Pattern):
        return lambda text: pattern.search(text) is not None
    raise TypeError(f"pattern must be str or re.Pattern, got {type(pattern).__name__}")


# ------------------------------ tag ambient ------------------------------
#
# ``logs.tag(**kwargs)`` 需要将标签附加到 with 块内产生的所有日志记录。
# 使用 ContextVar 实现协程/线程安全：不同线程/任务的标签互不影响。

try:
    from contextvars import ContextVar
except ImportError:  # pragma: no cover - Python >= 3.10 必有
    raise

_ambient_tags: "ContextVar[tuple[tuple[str, Any], ...]]" = ContextVar(
    "redpymake_ambient_tags", default=()
)


class SessionLogs:
    """暴露给用户的日志入口对象（``session.logs``）。

    对 ``LogBuffer`` 做轻量包装，隐藏内部序号/游标的写入细节，只暴露读取、
    tag、订阅、保存、cursor 等能力。
    """

    __slots__ = ("_buffer",)

    def __init__(self, buffer: LogBuffer) -> None:
        self._buffer = buffer

    @property
    def buffer(self) -> LogBuffer:
        """底层缓冲；库内部使用。"""
        return self._buffer

    def records(
        self,
        *,
        since: LogCursor | None = None,
        channel: str | None = None,
    ) -> list[SessionLogRecord]:
        return self._buffer.records(since=since, channel=channel)

    def text(
        self,
        *,
        since: LogCursor | None = None,
        channel: str | None = None,
        include_events: Iterable[str] | None = ("command_output",),
    ) -> str:
        return self._buffer.text(
            since=since, channel=channel, include_events=include_events
        )

    def cursor(self) -> LogCursor:
        return self._buffer.cursor()

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        return self._buffer.subscribe(callback)

    def save(
        self,
        path: str,
        *,
        channel: str | None = None,
        include_events: Iterable[str] | None = ("command_output",),
    ) -> None:
        """将日志文本写入本地文件。缺失父目录会自动创建。"""
        import os

        parent = os.path.dirname(os.fspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        content = self.text(channel=channel, include_events=include_events)
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(content)
            if content and not content.endswith("\n"):
                fp.write("\n")

    @contextmanager
    def tag(self, **fields: Any) -> Iterator[None]:
        """在 with 块内为所有日志记录附加标签字段。

        嵌套时内层覆盖同名外层键；离开 with 块自动恢复。
        """
        current = dict(_ambient_tags.get())
        current.update(fields)
        token = _ambient_tags.set(tuple(current.items()))
        try:
            yield
        finally:
            _ambient_tags.reset(token)


__all__ = [
    "SessionLogRecord",
    "LogBuffer",
    "LogCursor",
    "LogMatch",
    "SessionLogs",
    "Subscriber",
]
