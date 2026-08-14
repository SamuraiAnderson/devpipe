"""脚本对象与日志分流 (CORE-09)。

本模块提供：

- ``ScriptRun``：一次脚本运行的容器；作为上下文管理器使用。持有：
    - 合流缓冲 ``_merged``：既接收由 ``_ScriptLoggingHandler`` 转换来的用户
      ``logging.LogRecord``，也接收所有已登记 ``Session`` 通过
      ``SessionLogs.subscribe`` 转发过来的 ``SessionLogRecord`` 副本；
    - Session 注册表：按 ``session.session_id`` 去重，保存 ``unsubscribe`` 句柄；
    - 异常边界：``__exit__`` 见到未处理异常时按 ``dump_on_error`` 落盘。
- ``ScriptSnapshot`` / ``SessionInfo`` / ``ExceptionInfo``：只读的落盘/回调数据模型。
- ``_ScriptLoggingHandler``：把 ``LogRecord`` 转为 ``SessionLogRecord``
  （``event="user_log"``, ``stream="python"``）后 append 到 ``_merged``。
- ``_current_script``：``ContextVar``，供 ``Session.__init__`` 判断是否有活跃
  ``ScriptRun`` 并自动 ``attach``。
- ``script(...)``：顶级工厂。

设计要点见 doc/core-lib-requirements.md § CORE-09。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from ._logs import SessionLogRecord

if TYPE_CHECKING:  # pragma: no cover
    from ._session import Session


_diag_logger = logging.getLogger("redpymake")

# ``ContextVar`` 供 Session 构造时读取当前活跃 ScriptRun；仅存"最内层"的 run 引用。
_current_script: "ContextVar[ScriptRun | None]" = ContextVar(
    "redpymake_current_script", default=None
)

_LEVEL_NAMES = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

# 为 user_log 记录分配的 sequence 独立命名空间（不与任何 Session buffer 冲突）。
_user_log_seq = count(0)

_script_id_counter = count(1)


@dataclass(frozen=True)
class SessionInfo:
    """`ScriptSnapshot.sessions` 元素：登记 session 的静态元信息。"""

    id: str
    kind: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class ExceptionInfo:
    """脚本 ``__exit__`` 捕获的异常摘要。"""

    type: str
    message: str
    traceback: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "message": self.message, "traceback": self.traceback}


@dataclass(frozen=True)
class ScriptSnapshot:
    """``ScriptRun`` 的只读快照。

    - ``records`` 已按 ``(timestamp, session_id, sequence)`` 稳定排序；
    - ``exception`` 只有在 ``__exit__`` 触发落盘时才为非 ``None``；
    - 结构可被 callable sink 直接消费，也可序列化为 JSON。
    """

    name: str
    started_at: float
    ended_at: float | None
    records: tuple[SessionLogRecord, ...]
    sessions: tuple[SessionInfo, ...]
    exception: ExceptionInfo | None = None


class _ScriptLoggingHandler(logging.Handler):
    """标准 ``logging`` → ``ScriptRun._merged`` 桥。"""

    def __init__(self, run: "ScriptRun", level: int) -> None:
        super().__init__(level=level)
        # 使用弱引用避免 handler 长期抓住 ScriptRun。这里 run 生命周期由
        # ``with`` 管理，出块必卸 handler，因此普通引用即可（避免 GC 竞态）。
        self._run = run

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._run._append_user_log(record)
        except Exception:  # pragma: no cover - handler 不得向上抛
            self.handleError(record)


def _normalize_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str) and level.upper() in _LEVEL_NAMES:
        return getattr(logging, level.upper())
    raise ValueError(f"invalid log_level: {level!r}")


def _looks_like_file_path(spec: str) -> bool:
    """启发式：判断字符串路径是"单文件"还是"目录"。

    - 以路径分隔符结尾 → 目录
    - 有后缀（``Path(spec).suffix != ""``）→ 单文件
    - 否则默认按目录处理，避免用户传 ``logs`` 时被当成 ``./logs`` 文件写入。
    """
    if not spec:
        return False
    if spec.endswith(("/", "\\", os.sep)):
        return False
    return Path(spec).suffix != ""


class ScriptRun:
    """一次脚本运行的容器。仅可作为上下文管理器使用。

    详见模块 docstring 与 doc/core-lib-requirements.md § CORE-09。
    """

    __slots__ = (
        "_name",
        "_dump_on_error",
        "_log_level",
        "_loggers",
        "_handler",
        "_handler_targets",
        "_cv_token",
        "_lock",
        "_merged",
        "_sessions",
        "_forwarders",
        "_started_at",
        "_ended_at",
        "_active",
        "_script_session_id",
    )

    def __init__(
        self,
        name: str | None = None,
        *,
        dump_on_error: "str | os.PathLike[str] | bool | Callable[[ScriptSnapshot], None] | None" = None,
        log_level: str | int = "INFO",
        loggers: Sequence[str] | None = None,
    ) -> None:
        self._name = name or "script"
        self._dump_on_error = dump_on_error
        self._log_level = _normalize_level(log_level)
        self._loggers: tuple[str, ...] | None = tuple(loggers) if loggers is not None else None

        self._handler: _ScriptLoggingHandler | None = None
        self._handler_targets: list[logging.Logger] = []
        self._cv_token: Token | None = None
        self._lock = threading.Lock()
        self._merged: list[SessionLogRecord] = []
        self._sessions: dict[str, "Session"] = {}
        self._forwarders: dict[str, Callable[[], None]] = {}
        self._started_at: float = 0.0
        self._ended_at: float | None = None
        self._active = False
        # 用于给 user_log 记录标注一个稳定的 session_id（不与真实 Session 冲突）
        self._script_session_id = f"script:{self._name}#{next(_script_id_counter)}"

    # ------------------------------------------------------------------ 属性

    @property
    def name(self) -> str:
        return self._name

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def ended_at(self) -> float | None:
        return self._ended_at

    @property
    def active(self) -> bool:
        return self._active

    # -------------------------------------------------------------- 生命周期

    def __enter__(self) -> "ScriptRun":
        if self._active:
            raise RuntimeError("ScriptRun is not reentrant; create a new one instead")
        self._active = True
        self._started_at = time.time()
        # 1) 设置 ContextVar；Session 构造读到即自动 attach
        self._cv_token = _current_script.set(self)
        # 2) 装 logging handler
        self._install_handler()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # 1) 卸 handler + reset ContextVar（幂等）
        try:
            self._uninstall_handler()
        finally:
            if self._cv_token is not None:
                try:
                    _current_script.reset(self._cv_token)
                except (LookupError, ValueError):  # pragma: no cover - 非并发路径不会触发
                    pass
                self._cv_token = None

        # 2) detach 所有 session（取消订阅）
        self._detach_all()

        # 3) 记时
        self._ended_at = time.time()
        self._active = False

        # 4) 若有异常且 dump_on_error 已配置，触发落盘
        if exc is not None and self._dump_on_error not in (None, False):
            try:
                self._dump(exc_type, exc, tb)
            except Exception:  # pragma: no cover - 落盘失败不掩盖原异常
                _diag_logger.exception(
                    "ScriptRun.dump failed while handling %s", exc_type
                )

        # 5) 不吞异常
        return None

    # ------------------------------------------------------------ handler 侧

    def _install_handler(self) -> None:
        self._handler = _ScriptLoggingHandler(self, self._log_level)
        if self._loggers is None:
            targets = [logging.getLogger()]
        else:
            targets = [logging.getLogger(name) for name in self._loggers]
        for lg in targets:
            lg.addHandler(self._handler)
        self._handler_targets = targets

    def _uninstall_handler(self) -> None:
        if self._handler is None:
            return
        for lg in self._handler_targets:
            try:
                lg.removeHandler(self._handler)
            except Exception:  # pragma: no cover - 用户异常清理不应影响主流程
                pass
        self._handler_targets = []
        self._handler = None

    # ----------------------------------------------------------- Session 侧

    def attach(self, session: "Session") -> None:
        """登记一个 root Session；重复 attach 幂等。

        通过 ``session.logs.subscribe(...)`` 拷贝转发每条 ``SessionLogRecord`` 到
        ``self._merged``。视图（``at()``）与 root 共享 buffer，因此只需登记 root。
        """
        # 尽量拿到 root，避免 view 与 root 重复登记
        root = getattr(session, "root", session)
        sid = root.session_id
        with self._lock:
            if sid in self._sessions:
                return
            self._sessions[sid] = root
        unsub = root.logs.subscribe(self._on_session_record)
        with self._lock:
            self._forwarders[sid] = unsub

    def detach(self, session: "Session") -> None:
        """取消对某个 Session 的转发订阅；未登记时无操作。"""
        root = getattr(session, "root", session)
        sid = root.session_id
        with self._lock:
            unsub = self._forwarders.pop(sid, None)
            self._sessions.pop(sid, None)
        if unsub is not None:
            try:
                unsub()
            except Exception:  # pragma: no cover - 取消订阅不应影响主流程
                _diag_logger.exception("ScriptRun.detach unsubscribe failed")

    def _detach_all(self) -> None:
        # 出块时只需断掉订阅（防止会话之后再产生记录还漏进 merged），
        # 但 ``_sessions`` 是快照要用的元数据（回调 sink、目录包的 per-session
        # 文件都依赖它），必须保留。
        with self._lock:
            forwarders = list(self._forwarders.values())
            self._forwarders.clear()
        for unsub in forwarders:
            try:
                unsub()
            except Exception:  # pragma: no cover
                _diag_logger.exception("ScriptRun._detach_all unsubscribe failed")

    # ------------------------------------------------------------ 合流写入

    def _on_session_record(self, record: SessionLogRecord) -> None:
        """Session buffer 的订阅回调；追加到 merged。"""
        with self._lock:
            self._merged.append(record)

    def _append_user_log(self, record: logging.LogRecord) -> None:
        """logging handler 的转换出口。"""
        merged_fields: dict[str, Any] = {
            "logger": record.name,
            "pathname": record.pathname,
            "lineno": record.lineno,
            "funcName": record.funcName,
        }
        session_record = SessionLogRecord(
            timestamp=record.created,
            sequence=next(_user_log_seq),
            session_id=self._script_session_id,
            event="user_log",
            level=record.levelname,
            stream="python",
            message=record.getMessage(),
            operation_id=None,
            fields=merged_fields,
        )
        with self._lock:
            self._merged.append(session_record)

    # ----------------------------------------------------------------- 快照

    def snapshot(self, exception: ExceptionInfo | None = None) -> ScriptSnapshot:
        """返回稳定排序的 ``ScriptSnapshot``。可在 ``__exit__`` 前调用。"""
        with self._lock:
            records = tuple(
                sorted(
                    self._merged,
                    key=lambda r: (r.timestamp, r.session_id, r.sequence),
                )
            )
            sessions = tuple(
                SessionInfo(id=s.session_id, kind=s.kind, label=s.label)
                for s in self._sessions.values()
            )
        return ScriptSnapshot(
            name=self._name,
            started_at=self._started_at,
            ended_at=self._ended_at,
            records=records,
            sessions=sessions,
            exception=exception,
        )

    # ----------------------------------------------------------------- 落盘

    def _dump(self, exc_type, exc, tb) -> None:
        exc_info = ExceptionInfo(
            type=exc_type.__name__ if exc_type is not None else "Exception",
            message=str(exc),
            traceback="".join(traceback.format_exception(exc_type, exc, tb)),
        )
        snap = self.snapshot(exception=exc_info)

        target = self._dump_on_error
        if callable(target):
            target(snap)
            return

        spec = os.fspath(target)  # type: ignore[arg-type]
        if _looks_like_file_path(spec):
            _write_single_file(Path(spec), snap)
        else:
            _write_bundle_dir(Path(spec), snap)


# ---------------------------------------------------------------- 落盘实现


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="microseconds")


def _format_record_line(rec: SessionLogRecord) -> str:
    return (
        f"{_iso(rec.timestamp)} [{rec.level}] "
        f"[{rec.session_id}] {rec.event}: {rec.message}"
    )


def _format_user_log_line(rec: SessionLogRecord) -> str:
    logger_name = str(rec.fields.get("logger", "?")) if rec.fields else "?"
    return f"{_iso(rec.timestamp)} [{rec.level}] {logger_name}: {rec.message}"


def _format_exception_footer(exc: ExceptionInfo) -> str:
    return (
        f"\n---\n"
        f"exception: {exc.type}: {exc.message}\n"
        f"traceback:\n{exc.traceback}"
    )


def _safe_session_id(sid: str) -> str:
    out = []
    for ch in sid:
        if ch.isalnum() or ch in "-.":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "session"


def _write_single_file(path: Path, snap: ScriptSnapshot) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_format_record_line(r) for r in snap.records]
    body = "\n".join(lines)
    footer = _format_exception_footer(snap.exception) if snap.exception else ""
    content = body + ("\n" if body else "") + footer
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(content)


def _write_bundle_dir(root: Path, snap: ScriptSnapshot) -> None:
    ts = datetime.fromtimestamp(snap.started_at).strftime("%Y%m%dT%H%M%S")
    bundle = root / f"{snap.name}-{ts}"
    bundle.mkdir(parents=True, exist_ok=True)

    # all.log
    all_lines = [_format_record_line(r) for r in snap.records]
    all_body = "\n".join(all_lines)
    footer = _format_exception_footer(snap.exception) if snap.exception else ""
    (bundle / "all.log").write_text(
        all_body + ("\n" if all_body else "") + footer,
        encoding="utf-8",
        newline="\n",
    )

    # script.log（只含 user_log）
    script_lines = [
        _format_user_log_line(r) for r in snap.records if r.event == "user_log"
    ]
    (bundle / "script.log").write_text(
        "\n".join(script_lines) + ("\n" if script_lines else ""),
        encoding="utf-8",
        newline="\n",
    )

    # per-session
    grouped: dict[str, list[SessionLogRecord]] = {}
    session_ids = {s.id for s in snap.sessions}
    for r in snap.records:
        if r.session_id in session_ids:
            grouped.setdefault(r.session_id, []).append(r)
    for sid, recs in grouped.items():
        fname = f"{_safe_session_id(sid)}.log"
        (bundle / fname).write_text(
            "\n".join(_format_record_line(r) for r in recs) + ("\n" if recs else ""),
            encoding="utf-8",
            newline="\n",
        )

    # meta.json
    meta = {
        "name": snap.name,
        "started_at": snap.started_at,
        "ended_at": snap.ended_at,
        "exception": snap.exception.to_dict() if snap.exception else None,
        "sessions": [s.to_dict() for s in snap.sessions],
    }
    (bundle / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


# ------------------------------------------------------------------ 工厂


def script(
    name: str | None = None,
    *,
    dump_on_error: "str | os.PathLike[str] | bool | Callable[[ScriptSnapshot], None] | None" = None,
    log_level: str | int = "INFO",
    loggers: Sequence[str] | None = None,
) -> ScriptRun:
    """构造一次脚本运行（``ScriptRun``）。见 § CORE-09。"""
    return ScriptRun(
        name=name,
        dump_on_error=dump_on_error,
        log_level=log_level,
        loggers=loggers,
    )


__all__ = [
    "ScriptRun",
    "ScriptSnapshot",
    "SessionInfo",
    "ExceptionInfo",
    "script",
]
