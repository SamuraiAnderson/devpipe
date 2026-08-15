"""Workspace 与借出语义 (CORE-11)。

``rpm.workspace(root)`` 返回一个 ``Workspace`` 容器，负责：

1. **会话池**：按 key 懒创建 + 跨脚本复用；``__exit__`` 时按 ``auto_close_sessions``
   关掉所有池内会话；
2. **借出代理**：``_BorrowedSession`` 透传所有属性到真实会话，但 ``__exit__``
   为 no-op，避免脚本内 ``with rpm.wsl() as sh:`` 出块时把共享连接关掉；
3. **ContextVar 联动**：``__enter__`` / 工作线程执行脚本前设定 ``_active_workspace``，
   顶层工厂（``rpm.local()`` 等）读到即自动借出——脚本源码零改动；
4. **串行队列**：单工作线程 FIFO，用 ``importlib.reload`` 保证脚本每次 ▶ 都
   加载最新代码；异常被捕获写入 ``WorkspaceRun.exception``，不搞挂主进程；
5. **日志组**：一份 workspace 日志 = 一份 ``stream.ndjson``。该日志下**所有** run
   的记录顺序追加进同一个文件（不做 per-run 分片）；每次运行前把
   ``REDPYMAKE_LIVE_SINK`` 指向活跃日志的 stream，CORE-10 的 sink 自然生效。
   Workspace 在 sink 外侧追写 ``workspace.run.begin`` / ``workspace.run.end``
   元行界定 run 边界——run 摘要不单独持久化，一律从这两条元行重建。

规范来源：doc/core-lib-requirements.md § CORE-11。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence

from ._discover import ScriptCard, discover as _discover_fn
from ._logs import SessionLogRecord
from .exceptions import WorkspaceStoppedError

if TYPE_CHECKING:  # pragma: no cover
    from ._script import ScriptRun, ScriptSnapshot
    from ._session import Session


_diag_logger = logging.getLogger("redpymake")

_active_workspace: "ContextVar[Workspace | None]" = ContextVar(
    "redpymake_active_workspace", default=None
)


# ------------------------------------------------------------------ 数据模型


# 目录名格式：<YYYY-MM-DDTHHMMSS>-<6-hex>；纯字面，跨平台安全，用 ISO 时间前缀便于 sort。
_LOG_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}-[0-9a-f]{6}$")

# meta.json 的 schema 版本（后续升级用）
_LOG_META_SCHEMA = 2

# 一份日志组的唯一日志流文件名
_STREAM_FILENAME = "stream.ndjson"

# Workspace 在 CORE-10 sink 外侧追写的 run 边界元行 event 名
_EV_RUN_BEGIN = "workspace.run.begin"
_EV_RUN_END = "workspace.run.end"

# 有 begin 无 end 的残段（崩溃 / 断电 / 强杀留下）重建成这个状态
_STATUS_INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class WorkspaceLog:
    """一组同批次运行的容器（§CORE-11 日志组）。

    磁盘布局：``<logs_root>/<id>/{meta.json, stream.ndjson}``。该日志下所有 run
    的记录都顺序追加进同一份 ``stream.ndjson``；唯一活跃日志由
    ``<logs_root>/_active.json`` 指定。
    """

    id: str
    name: str
    created_at: float
    description: str
    pinned: bool
    root: Path
    run_count: int
    is_active: bool

    @property
    def stream_path(self) -> Path:
        """该日志组唯一的 NDJSON 流文件。"""
        return self.root / _STREAM_FILENAME

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "description": self.description,
            "pinned": self.pinned,
            "root": str(self.root),
            "stream_path": str(self.stream_path),
            "run_count": self.run_count,
            "is_active": self.is_active,
        }


@dataclass(frozen=True)
class WorkspaceRun:
    """Workspace 队列内一次脚本运行的记录。

    ``ndjson_path`` 指向该 run 所属日志组的 ``stream.ndjson``——**不是** run 独占的
    文件（日志组模型下多个 run 共享同一份流）。要拿单个 run 的记录请用
    ``Workspace.iter_run_records(run_id)``，它按 ``workspace.run.begin/end`` 元行
    切出该 run 的区间。
    """

    id: str
    script_path: Path
    script_name: str
    # "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted"
    status: str
    started_at: float | None
    ended_at: float | None
    exception: str | None
    ndjson_path: Path
    snapshot: "ScriptSnapshot | None" = None
    log_id: str = ""  # 该 run 所属日志组 id；默认空串仅供旧代码兼容，正常路径必赋值
    # 该 run 段在 stream.ndjson 里的字节区间；回放时可直接 seek 省掉全文件扫描
    stream_offset_begin: int | None = None
    stream_offset_end: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "script_path": str(self.script_path),
            "script_name": self.script_name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exception": self.exception,
            "ndjson_path": str(self.ndjson_path),
            "log_id": self.log_id,
            "stream_offset_begin": self.stream_offset_begin,
            "stream_offset_end": self.stream_offset_end,
        }


# ------------------------------------------------------------------ 借出代理


# 会真正"干活"的方法：在这些调用的入口做协作式停止检查。已经跑起来的子进程不强杀，
# 所以停止只在**下一次**调用这些方法时生效。
_GUARDED_METHODS = frozenset({"run", "wait", "push", "pull", "copy"})


class _BorrowedSession:
    """Workspace 借出会话时的透明代理。

    - 属性/方法完全透传给真会话；
    - ``__enter__`` 返回**代理自身**——这样 ``with rpm.local() as sess:`` 里的
      ``sess.run(...)`` 依旧经过代理，协作式停止检查才有机会生效；代理对属性访问
      完全透明，用户感知不到差别；
    - ``__exit__`` **不 close** 真会话（Workspace 拥有生命周期）；
    - ``root``/``session_id``/``kind``/``label`` 等属性从真会话拿；
    - ``at()`` 返回的视图会被重新包一层代理，避免 ``sess.at(x).run(...)`` 绕过检查。
    """

    __slots__ = ("_real", "_ws")

    def __init__(self, real: "Session", ws: "Workspace | None" = None) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_ws", ws)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        ws = self._ws
        if ws is None or not callable(attr):
            return attr
        if name in _GUARDED_METHODS:
            return _make_stop_guard(ws, name, attr)
        if name == "at":
            return _make_view_wrapper(ws, attr)
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_real", "_ws"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def __enter__(self) -> "_BorrowedSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # 不 close：Workspace 拥有生命周期
        return None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<Borrowed {self._real!r}>"


def _make_stop_guard(ws: "Workspace", name: str, fn: Callable[..., Any]):
    """包一层：调真方法之前先看 workspace 是否已请求停止。"""

    def _guarded(*args: Any, **kwargs: Any) -> Any:
        ws._check_stop_requested(name)
        return fn(*args, **kwargs)

    return _guarded


def _make_view_wrapper(ws: "Workspace", fn: Callable[..., Any]):
    """包一层：``at()`` 产出的视图重新套上借出代理，让守卫沿着视图链传播。"""

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        view = fn(*args, **kwargs)
        return _BorrowedSession(view, ws)

    return _wrapped


# ------------------------------------------------------------------ Workspace


_ws_id_counter = count(1)


class Workspace:
    """WebUI / REPL 模式下的运行时容器。见模块 docstring 与 § CORE-11。"""

    def __init__(
        self,
        root: str | os.PathLike[str] = ".",
        *,
        logs_root: str | os.PathLike[str] | None = None,
        ndjson_dir: str | os.PathLike[str] | None = None,  # 兼容旧参数名；等价于 logs_root
        discovery_patterns: Sequence[str] | None = None,
        auto_close_sessions: bool = True,
        log_name: str | None = None,
        new_log_on_start: bool = False,
    ) -> None:
        self._root: Path = Path(os.fspath(root)).resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"workspace root not found: {self._root}")
        # logs_root 是所有日志组的父目录；ndjson_dir 保留为兼容别名
        _logs_arg = logs_root if logs_root is not None else ndjson_dir
        self._logs_root: Path = (
            Path(os.fspath(_logs_arg))
            if _logs_arg is not None
            else self._root / ".redpymake" / "logs"
        )
        self._discovery_patterns: tuple[str, ...] | None = (
            tuple(discovery_patterns) if discovery_patterns is not None else None
        )
        self._auto_close = auto_close_sessions
        self._initial_log_name = log_name
        self._new_log_on_start = new_log_on_start

        # 会话池：key -> real Session
        self._pool: dict[str, "Session"] = {}
        self._pool_lock = threading.Lock()

        # 队列
        self._queue: deque[str] = deque()
        self._queue_lock = threading.Lock()
        self._queue_cond = threading.Condition(self._queue_lock)
        self._paused = False

        # 状态：runs 存储的是"当前活跃日志"的 runs；历史日志的 runs 按需惰性加载到 _extra_runs
        self._runs: dict[str, WorkspaceRun] = {}
        self._runs_order: list[str] = []
        self._extra_runs: dict[str, WorkspaceRun] = {}      # 非活跃日志 rid -> run
        self._loaded_history_logs: set[str] = set()          # 已惰性载入 index 的历史 log_id
        self._runs_lock = threading.Lock()
        self._current_run_id: str | None = None
        self._run_id_counter = count(1)
        self._loaded_modules: dict[str, ModuleType] = {}
        self._loaded_modules_lock = threading.Lock()

        # 日志组
        self._logs: dict[str, WorkspaceLog] = {}
        self._active_log_id: str | None = None
        self._logs_lock = threading.RLock()
        # 保护 workspace 侧对 stream.ndjson 的元行追写；sink 有自己的锁，两者都是
        # append 模式且串行执行，不会交错半行
        self._stream_lock = threading.Lock()

        # 协作式停止：stop_current() 置起，借出会话在命令边界检查后抛异常
        self._stop_requested = False

        self._worker: threading.Thread | None = None
        self._cv_token = None
        self._closed = False
        self._active = False
        self._instance_key = next(_ws_id_counter)

        # 订阅（供 UI 使用）
        self._subscribers: list[Callable[[dict], None]] = []
        self._subscribers_lock = threading.Lock()

    # ------------------------------------------------------ 生命周期

    @property
    def root(self) -> Path:
        return self._root

    @property
    def logs_root(self) -> Path:
        return self._logs_root

    @property
    def ndjson_dir(self) -> Path:
        """Backward-compat alias for :attr:`logs_root` (§CORE-11 日志组前的旧字段名)。"""
        return self._logs_root

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active(self) -> bool:
        return self._active

    def __enter__(self) -> "Workspace":
        if self._active:
            raise RuntimeError("Workspace is already active")
        self._logs_root.mkdir(parents=True, exist_ok=True)
        # 扫盘恢复日志组（含 _active.json）；无历史时创建首个默认日志
        self._bootstrap_logs()
        self._closed = False
        self._active = True
        # 启动工作线程
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"rpm-workspace-{self._instance_key}",
            daemon=True,
        )
        self._worker.start()
        # 主线程设定 ContextVar；顶层工厂读到即借出
        self._cv_token = _active_workspace.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # 通知工作线程退出
        with self._queue_cond:
            self._queue_cond.notify_all()
        if self._worker is not None:
            try:
                self._worker.join(timeout=15)
            except Exception:  # pragma: no cover - 工作线程 join 失败仅记录
                _diag_logger.exception("workspace worker join failed")
            self._worker = None
        # 关闭会话池
        if self._auto_close:
            with self._pool_lock:
                sessions = list(self._pool.values())
                self._pool.clear()
            for sess in sessions:
                try:
                    sess.close()
                except Exception:  # pragma: no cover
                    _diag_logger.exception("failed to close pooled session %r", sess)
        # 复位 ContextVar
        if self._cv_token is not None:
            try:
                _active_workspace.reset(self._cv_token)
            except (LookupError, ValueError):  # pragma: no cover
                pass
            self._cv_token = None
        self._active = False

    # ------------------------------------------------------ 会话池

    def sessions(self) -> Mapping[str, "Session"]:
        with self._pool_lock:
            return dict(self._pool)

    def _get_or_create(
        self, key: str, factory: Callable[[], "Session"]
    ) -> _BorrowedSession:
        """通用池取用；若已缓存则复用（并尝试补 attach 到当前 ScriptRun），否则新建。"""
        with self._pool_lock:
            existing = self._pool.get(key)
            if existing is not None and not existing.closed:
                self._maybe_attach_to_current_script(existing)
                return _BorrowedSession(existing, self)
        # 新建放在锁外，避免 __init__ 里的 ContextVar 读取与其他锁交互；
        # 若并发拿同一 key，取"先到者"，晚到者关掉自己。
        sess = factory()
        with self._pool_lock:
            already = self._pool.get(key)
            if already is not None and not already.closed:
                # 并发命中：舍弃刚新建的（关掉），复用旧的
                try:
                    sess.close()
                except Exception:  # pragma: no cover
                    _diag_logger.exception("workspace: failed to close redundant session")
                self._maybe_attach_to_current_script(already)
                return _BorrowedSession(already, self)
            self._pool[key] = sess
        return _BorrowedSession(sess, self)

    def _maybe_attach_to_current_script(self, session: "Session") -> None:
        """如果当前有活跃 ScriptRun，把 session 补挂到它的 subscribe 上（幂等）。"""
        try:
            from ._script import _current_script
        except ImportError:  # pragma: no cover
            return
        run = _current_script.get()
        if run is not None:
            try:
                run.attach(session)
            except Exception:  # pragma: no cover - attach 失败仅记录
                _diag_logger.exception("workspace: attach to current script failed")

    def local(self, *, default_cwd: str | None = None) -> _BorrowedSession:
        from ._local import LocalSession

        return self._get_or_create(
            "local", lambda: LocalSession(default_cwd=default_cwd)
        )

    def ssh(
        self,
        host: str,
        *,
        user: str | None = None,
        port: int = 22,
        password: str | None = None,
        key_filename: str | os.PathLike[str] | None = None,
        default_cwd: str | None = None,
        connect_timeout: float = 15.0,
    ) -> _BorrowedSession:
        from ._ssh import SshSession

        key = f"ssh:{user or 'default'}@{host}:{port}"
        return self._get_or_create(
            key,
            lambda: SshSession(
                host,
                user=user,
                port=port,
                password=password,
                key_filename=key_filename,
                default_cwd=default_cwd,
                connect_timeout=connect_timeout,
            ),
        )

    def adb(
        self,
        serial: str | None = None,
        *,
        adb_path: str | None = None,
        default_cwd: str | None = None,
        connect_timeout: float = 10.0,
    ) -> _BorrowedSession:
        from ._adb import AdbSession

        key = f"adb:{serial or 'default'}"
        return self._get_or_create(
            key,
            lambda: AdbSession(
                serial=serial,
                adb_path=adb_path,
                default_cwd=default_cwd,
                connect_timeout=connect_timeout,
            ),
        )

    def serial(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 1.0,
        newline: str = "\r\n",
        encoding: str = "utf-8",
    ) -> _BorrowedSession:
        from ._serial import SerialSession

        key = f"serial:{port}@{baudrate}"
        return self._get_or_create(
            key,
            lambda: SerialSession(
                port,
                baudrate=baudrate,
                timeout=timeout,
                newline=newline,
                encoding=encoding,
            ),
        )

    def wsl(
        self,
        distribution: str | None = None,
        *,
        user: str | None = None,
        wsl_path: str | None = None,
        default_cwd: str | None = None,
    ) -> _BorrowedSession:
        from ._wsl import WslSession

        key = f"wsl:{distribution or 'default'}"
        return self._get_or_create(
            key,
            lambda: WslSession(
                distribution=distribution,
                user=user,
                wsl_path=wsl_path,
                default_cwd=default_cwd,
            ),
        )

    # ------------------------------------------------------ 发现

    def discover(self) -> list[ScriptCard]:
        return _discover_fn(self._root, patterns=self._discovery_patterns)

    def refresh(self) -> None:
        """重扫；接口保留兼容位，当前 discover() 每次即时扫描无需缓存刷新。"""

    # ------------------------------------------------------ 日志组

    @property
    def current_log(self) -> WorkspaceLog:
        """当前活跃日志（新 run 的写入目标）。"""
        with self._logs_lock:
            if self._active_log_id is None:
                raise RuntimeError("workspace is not active; enter the context first")
            log = self._logs.get(self._active_log_id)
        if log is None:
            raise RuntimeError("workspace active log missing from registry")
        return log

    def list_logs(self) -> list[WorkspaceLog]:
        """按 ``created_at`` 逆序（新→旧）返回全部日志组。"""
        with self._logs_lock:
            logs = list(self._logs.values())
        logs.sort(key=lambda x: x.created_at, reverse=True)
        return logs

    def get_log(self, log_id: str) -> WorkspaceLog:
        with self._logs_lock:
            log = self._logs.get(log_id)
        if log is None:
            raise KeyError(f"log not found: {log_id}")
        return log

    def rename_log(
        self, log_id: str, name: str, description: str | None = None
    ) -> WorkspaceLog:
        """给日志组改显示名 / 描述。空 name 拒绝，避免"匿名日志"。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("log name cannot be empty")
        with self._logs_lock:
            log = self._logs.get(log_id)
            if log is None:
                raise KeyError(f"log not found: {log_id}")
            new_desc = log.description if description is None else description.strip()
            updated = replace(log, name=name, description=new_desc)
            self._logs[log_id] = updated
            _write_log_meta(updated)
        self._emit_event(
            {"type": "log.renamed", "log_id": log_id, "name": name, "description": new_desc}
        )
        return updated

    def rotate_log(self, name: str | None = None) -> WorkspaceLog:
        """归档当前日志（不删除）并起一份新的活跃日志。

        ``current_run`` 非空时拒绝，避免"跑到一半换存储目标"引起的 NDJSON 分裂；
        调用方（Web UI）应先提示等或使用 ``pause_queue`` / 等 run 跑完再 rotate。
        """
        if not self._active:
            raise RuntimeError("workspace is not active")
        if self.current_run is not None:
            raise RuntimeError(
                "cannot rotate: a run is in progress; wait for it to finish or stop the queue first"
            )
        # 队列里若有排队但未跑的 run，也拒绝——它们已绑定旧 log_id 的 ndjson_path
        with self._queue_cond:
            queued_count = len(self._queue)
        if queued_count > 0:
            raise RuntimeError(
                f"cannot rotate: {queued_count} run(s) still queued for the current log"
            )

        with self._logs_lock:
            previous_id = self._active_log_id
            new_log = _create_new_log(self._logs_root, name)
            self._logs[new_log.id] = new_log
            # 旧活跃日志：把 is_active 标记为 False
            if previous_id and previous_id in self._logs:
                self._logs[previous_id] = replace(
                    self._logs[previous_id], is_active=False
                )
            self._logs[new_log.id] = replace(new_log, is_active=True)
            self._active_log_id = new_log.id
            _write_active_log_pointer(self._logs_root, new_log.id)
        # 内存里 runs 视图切到新（空）日志
        with self._runs_lock:
            # 旧活跃 run 全部下沉到 _extra_runs（历史访问用）
            for rid, run in list(self._runs.items()):
                self._extra_runs[rid] = run
            self._runs.clear()
            self._runs_order.clear()
        with self._logs_lock:
            active = self._logs[new_log.id]
        self._emit_event(
            {"type": "log.rotated", "log_id": active.id, "previous_log_id": previous_id}
        )
        return active

    def pin_log(self, log_id: str, pinned: bool = True) -> WorkspaceLog:
        with self._logs_lock:
            log = self._logs.get(log_id)
            if log is None:
                raise KeyError(f"log not found: {log_id}")
            updated = replace(log, pinned=bool(pinned))
            self._logs[log_id] = updated
            _write_log_meta(updated)
        self._emit_event({"type": "log.pinned", "log_id": log_id, "pinned": bool(pinned)})
        return updated

    def discard_log(self, log_id: str) -> None:
        """硬删一份日志组。活跃或 ``pinned`` 时拒绝。"""
        with self._logs_lock:
            log = self._logs.get(log_id)
            if log is None:
                raise KeyError(f"log not found: {log_id}")
            if log.is_active:
                raise RuntimeError(
                    "cannot discard the active log; rotate to a new one first"
                )
            if log.pinned:
                raise RuntimeError(
                    f"cannot discard pinned log {log_id!r}; unpin first"
                )
            del self._logs[log_id]
            try:
                _rmtree_safe(log.root)
            except Exception:  # pragma: no cover - 磁盘错误仅记录
                _diag_logger.exception("failed to remove log dir %s", log.root)
        with self._runs_lock:
            for rid in [r for r, run in self._extra_runs.items() if run.log_id == log_id]:
                self._extra_runs.pop(rid, None)
            self._loaded_history_logs.discard(log_id)
        self._emit_event({"type": "log.discarded", "log_id": log_id})

    def list_runs_in_log(self, log_id: str) -> list[WorkspaceRun]:
        """返回某个日志组里的 runs（按 stream 中出现顺序）。

        活跃日志直接读内存；历史日志惰性扫它的 ``stream.ndjson``（按
        ``workspace.run.begin/end`` 元行配对重建）并缓存到 ``_extra_runs``。
        """
        with self._logs_lock:
            log = self._logs.get(log_id)
        if log is None:
            raise KeyError(f"log not found: {log_id}")
        if log.is_active:
            with self._runs_lock:
                return [self._runs[rid] for rid in self._runs_order if rid in self._runs]
        # 历史日志：命中缓存则跳过磁盘
        with self._runs_lock:
            if log_id in self._loaded_history_logs:
                return [r for r in self._extra_runs.values() if r.log_id == log_id]
        # 冷路径：扫 stream 重建
        runs = _rebuild_runs_from_stream(log)
        with self._runs_lock:
            for r in runs:
                self._extra_runs.setdefault(r.id, r)
            self._loaded_history_logs.add(log_id)
        return runs

    # ------------------------------------------------------ 队列

    def enqueue(self, path: str | os.PathLike[str]) -> str:
        if self._closed:
            raise RuntimeError("workspace is closed")
        script_path = Path(os.fspath(path)).resolve()
        script_name = self._script_name_for(script_path)
        rid = f"{script_name}#{next(self._run_id_counter)}"
        # 所有 run 共享活跃日志的 stream.ndjson；活跃 log 在 __enter__ 里已确保存在
        with self._logs_lock:
            active_id = self._active_log_id
            active_log = self._logs.get(active_id) if active_id else None
        if active_log is None:  # pragma: no cover - __enter__ 后不应发生
            raise RuntimeError("workspace has no active log; did you call __enter__?")
        active_log.root.mkdir(parents=True, exist_ok=True)
        ndjson_path = active_log.stream_path
        run = WorkspaceRun(
            id=rid,
            script_path=script_path,
            script_name=script_name,
            status="queued",
            started_at=None,
            ended_at=None,
            exception=None,
            ndjson_path=ndjson_path,
            snapshot=None,
            log_id=active_log.id,
        )
        with self._runs_lock:
            self._runs[rid] = run
            self._runs_order.append(rid)
        with self._queue_cond:
            self._queue.append(rid)
            self._queue_cond.notify()
        self._emit_event({"type": "run.enqueued", "run_id": rid, "log_id": active_log.id})
        return rid

    def stop_current(self) -> bool:
        """请求终止当前 run（协作式，尽力而为）。

        置起 ``_stop_requested`` 标志：借出会话在每次 ``run()`` / ``wait()`` **进入
        前**检查它，已置起则抛 :class:`WorkspaceStoppedError`，让脚本在**下一条命令
        的边界**退出。**已经跑起来的子进程不强杀**——一条长命令会跑完。

        run 收尾时若标志被置起过，``status`` 覆盖为 ``"cancelled"``。

        Returns:
            ``True`` 表示确实有 run 在跑、请求已登记；``False`` 表示当前空闲。
        """
        with self._runs_lock:
            rid = self._current_run_id
        if rid is None:
            return False
        self._stop_requested = True
        self._emit_event({"type": "run.stopping", "run_id": rid})
        return True

    def cancel_run(self, run_id: str) -> bool:
        """把一个仍在 ``queued`` 的 run 剔出队列并标 ``cancelled``。

        只对排队中的 run 有效——已经在跑的请用 :meth:`stop_current`。被取消的 run
        从未产生记录，因此**不落盘 stream**（重开 workspace 后它不会出现）。

        Returns:
            ``True`` 表示确实取消了；``False`` 表示该 run 不存在或已不是 queued。
        """
        with self._queue_cond:
            if run_id not in self._queue:
                return False
            # deque.remove 是 O(n)，队列长度是人手点出来的量级，可接受
            self._queue.remove(run_id)
        with self._runs_lock:
            run = self._runs.get(run_id)
            if run is not None and run.status == "queued":
                self._runs[run_id] = replace(
                    run, status="cancelled", ended_at=time.time()
                )
        self._emit_event({"type": "run.cancelled", "run_id": run_id})
        return True

    def rerun(self, run_id: str) -> str:
        """用同一脚本路径重新入队，返回新的 run_id。原 run 记录保持不动。"""
        run = self.get_run(run_id)
        return self.enqueue(run.script_path)

    def pause_queue(self) -> None:
        with self._queue_cond:
            self._paused = True

    def resume_queue(self) -> None:
        with self._queue_cond:
            self._paused = False
            self._queue_cond.notify_all()

    def clear_queue(self) -> None:
        with self._queue_cond:
            drained_ids = list(self._queue)
            self._queue.clear()
        with self._runs_lock:
            for rid in drained_ids:
                run = self._runs.get(rid)
                if run is not None and run.status == "queued":
                    self._runs[rid] = replace(run, status="cancelled")

    @property
    def current_run(self) -> WorkspaceRun | None:
        with self._runs_lock:
            rid = self._current_run_id
            if rid is None:
                return None
            return self._runs.get(rid)

    @property
    def runs(self) -> Sequence[WorkspaceRun]:
        with self._runs_lock:
            return [self._runs[rid] for rid in self._runs_order]

    def get_run(self, run_id: str) -> WorkspaceRun:
        with self._runs_lock:
            if run_id in self._runs:
                return self._runs[run_id]
            if run_id in self._extra_runs:
                return self._extra_runs[run_id]
        # 未命中：扫历史日志（惰性从它们的 stream 重建）
        for log in self.list_logs():
            if log.is_active:
                continue
            for r in self.list_runs_in_log(log.id):
                if r.id == run_id:
                    return r
        raise KeyError(run_id)

    def iter_run_records(self, run_id: str) -> Iterator[dict]:
        """切出某个 run 在其所属日志 stream 里的记录区间。

        区间锚点是 ``workspace.run.begin/end`` 元行（两条本身也会 yield——前端
        timeline 需要它们画 run 分隔）。若 run 已知字节偏移则直接 seek，否则顺序扫。
        """
        run = self.get_run(run_id)
        stream = run.ndjson_path
        if not stream.exists():
            return iter([])
        return _iter_run_slice(
            stream,
            run_id,
            offset_begin=run.stream_offset_begin,
            offset_end=run.stream_offset_end,
        )

    # ------------------------------------------------------ 订阅

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._subscribers_lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._subscribers_lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    def _emit_event(self, payload: dict) -> None:
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(payload)
            except Exception:  # pragma: no cover - 用户回调不能拖挂 workspace
                _diag_logger.exception("workspace subscriber raised")

    def _make_script_record_hook(
        self, script_run: "ScriptRun"
    ) -> Callable[[SessionLogRecord], None] | None:
        """返回一个把 ``SessionLogRecord`` 序列化并广播给订阅者的回调。

        由 ``ScriptRun.__enter__`` 在检测到活跃 Workspace 时调用；当且仅当当前
        Workspace 正在跑一个 run（``_current_run_id`` 非空）时才绑定，避免
        REPL 场景下无 run 归属的记录满天飞。
        """
        with self._runs_lock:
            rid = self._current_run_id
        if rid is None:
            return None

        def _hook(record: SessionLogRecord) -> None:
            self._emit_event(
                {
                    "type": "run.record",
                    "run_id": rid,
                    "record": _record_to_dict(record),
                }
            )

        return _hook

    # ------------------------------------------------------ 工作线程

    def _worker_loop(self) -> None:
        while True:
            with self._queue_cond:
                while not self._queue and not self._closed and not self._paused:
                    self._queue_cond.wait(timeout=0.5)
                if self._closed:
                    return
                if self._paused:
                    continue
                if not self._queue:
                    continue
                rid = self._queue.popleft()
            try:
                self._execute_run(rid)
            except Exception:  # pragma: no cover - 工作循环自身不能崩
                _diag_logger.exception("workspace worker loop error")

    def _execute_run(self, rid: str) -> None:
        with self._runs_lock:
            run = self._runs.get(rid)
            if run is None:
                return
            started = time.time()
            self._runs[rid] = replace(run, status="running", started_at=started)
            self._current_run_id = rid
        # 新 run 开始前重置协作式停止标志
        self._stop_requested = False
        self._emit_event({"type": "run.started", "run_id": rid})

        stream_path = run.ndjson_path  # = 活跃日志的 stream.ndjson

        # run 段起始元行；同时记下字节偏移供回放 seek
        begin_meta = {
            "event": _EV_RUN_BEGIN,
            "run_id": rid,
            "script_path": str(run.script_path),
            "script_name": run.script_name,
            "log_id": run.log_id,
            "started_at": started,
            "timestamp": started,
        }
        offset_begin = self._append_stream_meta(stream_path, begin_meta)
        # 元行单独走 run.meta 通道（run.record 的 payload 契约是 SessionLogRecord，
        # 不能混进来）；live tail 靠它画 run 分隔条，实时看和回放看拿到同一串数据
        self._emit_event({"type": "run.meta", "run_id": rid, "record": begin_meta})

        # 环境变量：让 CORE-10 sink 追加到同一条 stream（sink 本来就是 append 模式）
        prev_env = os.environ.get("REDPYMAKE_LIVE_SINK")
        os.environ["REDPYMAKE_LIVE_SINK"] = f"file://{stream_path.as_posix()}"

        # 在工作线程内设定 ContextVar：脚本内的 rpm.local() 等借出共享会话
        token = _active_workspace.set(self)

        status = "succeeded"
        exception_text: str | None = None
        try:
            module = self._load_or_reload_module(run.script_path)
            main = getattr(module, "main", None)
            if not callable(main):
                raise RuntimeError(
                    f"entry_missing: {run.script_path} has no callable main()"
                )
            main()
        except WorkspaceStoppedError as exc:
            # 协作式停止走到了命令边界；不算失败
            status = "cancelled"
            exception_text = f"{type(exc).__name__}: {exc}"
            _diag_logger.info("workspace run %s stopped by user", rid)
        except BaseException as exc:  # 捕获包含 SystemExit
            status = "failed"
            exception_text = f"{type(exc).__name__}: {exc}"
            _diag_logger.info("workspace run %s failed: %s", rid, exception_text)
        finally:
            try:
                _active_workspace.reset(token)
            except (LookupError, ValueError):  # pragma: no cover
                pass
            # 恢复环境变量
            if prev_env is None:
                os.environ.pop("REDPYMAKE_LIVE_SINK", None)
            else:
                os.environ["REDPYMAKE_LIVE_SINK"] = prev_env
            # 用户按过 stop：即便脚本自己正常返回，也如实记成 cancelled
            if self._stop_requested and status == "succeeded":
                status = "cancelled"
                exception_text = exception_text or "stopped by user"
            ended = time.time()
            # run 段结束元行；写完后的文件长度即该 run 段的右边界
            end_meta = {
                "event": _EV_RUN_END,
                "run_id": rid,
                "status": status,
                "ended_at": ended,
                "exception": exception_text,
                "timestamp": ended,
            }
            offset_end = self._append_stream_meta(
                stream_path, end_meta, return_end_offset=True
            )
            self._emit_event({"type": "run.meta", "run_id": rid, "record": end_meta})
            with self._runs_lock:
                cur = self._runs.get(rid)
                if cur is not None:
                    self._runs[rid] = replace(
                        cur,
                        status=status,
                        started_at=started,
                        ended_at=ended,
                        exception=exception_text,
                        stream_offset_begin=offset_begin,
                        stream_offset_end=offset_end,
                    )
                self._current_run_id = None
            self._stop_requested = False
            # run_count 是从 stream 派生的，这里同步递增内存副本
            self._bump_log_run_count(run.log_id)
        self._emit_event({"type": "run.finished", "run_id": rid, "status": status})

    def _load_or_reload_module(self, path: Path) -> ModuleType:
        """每次运行都刷新代码。

        动态 ``spec_from_file_location`` 创建的模块没法可靠地被 ``importlib.reload``
        重新读盘（``reload`` 走的是包路径 finder，动态模块的名字并不对应任何包路径），
        因此我们每次都用 ``spec.loader.exec_module`` 重新加载，得到含最新代码的
        全新模块对象。旧的 sys.modules 项被替换。
        """
        mod_key = f"__rpm_ws_{self._instance_key}_{abs(hash(str(path)))}__"
        module = self._fresh_load_module(mod_key, path)
        with self._loaded_modules_lock:
            self._loaded_modules[mod_key] = module
        return module

    def _fresh_load_module(self, mod_key: str, path: Path) -> ModuleType:
        """直接从磁盘读源码 + compile + exec 到新模块对象。

        绕开 ``SourceFileLoader`` 的字节码缓存——它在同一进程内会因 mtime 精度问题
        (Windows / 快速连续写入) 而返回旧代码。我们自己读文件保证每次拿到最新
        源码。副作用：不生成 ``.pyc``（可以接受，脚本运行不追求启动性能）。
        """
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read script {path}: {exc}") from exc
        try:
            code = compile(source, str(path), "exec")
        except SyntaxError as exc:
            raise RuntimeError(f"syntax error in {path}: {exc}") from exc
        module = ModuleType(mod_key)
        module.__file__ = str(path)  # 让 traceback 指到真源码
        module.__name__ = mod_key
        module.__package__ = None
        sys.modules[mod_key] = module
        try:
            exec(code, module.__dict__)
        except Exception:
            sys.modules.pop(mod_key, None)
            raise
        return module

    # ------------------------------------------------------ 日志组内部

    def _bootstrap_logs(self) -> None:
        """``__enter__`` 时调；扫盘恢复日志组 + 加载活跃日志的 runs。

        ``new_log_on_start`` 为真时，恢复出来的活跃日志若已有 run 就另起一份新的，
        旧的降级为历史（仍出现在 ``list_logs()`` 与 UI 下拉里）。
        """
        # 1) 扫已存在的 log 目录 → meta.json
        found: dict[str, WorkspaceLog] = {}
        if self._logs_root.exists():
            for entry in sorted(self._logs_root.iterdir()):
                if not entry.is_dir() or not _LOG_ID_RE.match(entry.name):
                    continue
                meta = _read_log_meta(entry)
                if meta is None:
                    continue
                run_count = _count_runs_in_stream(entry)
                found[entry.name] = WorkspaceLog(
                    id=entry.name,
                    name=meta.get("name") or entry.name,
                    created_at=float(meta.get("created_at", 0.0)),
                    description=meta.get("description", ""),
                    pinned=bool(meta.get("pinned", False)),
                    root=entry,
                    run_count=run_count,
                    is_active=False,
                )

        # 2) 读 _active.json → 决定 active log id
        active_id: str | None = None
        pointer = self._logs_root / "_active.json"
        if pointer.exists():
            try:
                data = json.loads(pointer.read_text(encoding="utf-8"))
                cand = data.get("log_id")
                if isinstance(cand, str) and cand in found:
                    active_id = cand
            except Exception:  # pragma: no cover - pointer 损坏 → 走回退
                _diag_logger.exception("failed to read %s; will fallback", pointer)

        # 3) fallback：pointer 缺失/损坏时，用最新的一个；再没有就新建
        if active_id is None:
            if found:
                active_id = sorted(found.keys())[-1]  # 时间戳 id 天然按字典序 == 时间序
            else:
                fresh = _create_new_log(self._logs_root, self._initial_log_name)
                found[fresh.id] = fresh
                active_id = fresh.id

        # 3.5) serve 语境：每次启动开一份新日志，上次那份留作历史。
        #      上次那份是空的（0 run）就直接接着用，免得反复重启堆一串空日志。
        if self._new_log_on_start and found[active_id].run_count > 0:
            fresh = _create_new_log(self._logs_root, self._initial_log_name)
            found[fresh.id] = fresh
            active_id = fresh.id

        # 4) 落 pointer + 标 is_active
        _write_active_log_pointer(self._logs_root, active_id)
        found[active_id] = replace(found[active_id], is_active=True)

        # 5) 扫活跃日志的 stream.ndjson 重建 self._runs
        active_log = found[active_id]
        active_runs = _rebuild_runs_from_stream(active_log)
        with self._runs_lock:
            self._runs.clear()
            self._runs_order.clear()
            for r in active_runs:
                self._runs[r.id] = r
                self._runs_order.append(r.id)
            # run_id_counter 递增到已存在 runs 之后，避免和历史 rid 撞
            max_seq = 0
            for rid in self._runs_order:
                _, _, num = rid.rpartition("#")
                if num.isdigit():
                    max_seq = max(max_seq, int(num))
            self._run_id_counter = count(max_seq + 1)

        with self._logs_lock:
            self._logs = found
            self._active_log_id = active_id

    def _append_stream_meta(
        self,
        stream_path: Path,
        payload: dict,
        *,
        return_end_offset: bool = False,
    ) -> int | None:
        """往日志流追写一条 workspace 元行；返回它的字节偏移。

        Args:
            return_end_offset: ``False`` 返回该行**写入前**的偏移（即行首），
                ``True`` 返回**写入后**的文件长度（即行尾）。前者给
                ``stream_offset_begin``，后者给 ``stream_offset_end``。

        写失败只记诊断日志、返回 ``None``——落盘问题不该让 run 挂掉。
        """
        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self._stream_lock:
                stream_path.parent.mkdir(parents=True, exist_ok=True)
                with open(stream_path, "a", encoding="utf-8", newline="\n") as fp:
                    fp.seek(0, os.SEEK_END)
                    before = fp.tell()
                    fp.write(line + "\n")
                    fp.flush()
                    after = fp.tell()
            return after if return_end_offset else before
        except OSError:  # pragma: no cover - 磁盘错误仅记录
            _diag_logger.exception("failed to append meta line to %s", stream_path)
            return None

    def _bump_log_run_count(self, log_id: str) -> None:
        """``run_count`` 是从 stream 派生的；run 收尾时同步递增内存副本。"""
        if not log_id:
            return
        with self._logs_lock:
            cur = self._logs.get(log_id)
            if cur is not None:
                self._logs[log_id] = replace(cur, run_count=cur.run_count + 1)

    def _check_stop_requested(self, where: str) -> None:
        """借出会话在命令边界调；已请求停止则抛 :class:`WorkspaceStoppedError`。"""
        if not self._stop_requested:
            return
        with self._runs_lock:
            rid = self._current_run_id
        raise WorkspaceStoppedError(
            f"run stopped by user before {where}()", run_id=rid
        )

    def _script_name_for(self, path: Path) -> str:
        """尽力从 ScriptCard 拿脚本名；退化到文件 stem。"""
        try:
            cards = self.discover()
        except Exception:  # pragma: no cover
            return path.stem
        for card in cards:
            try:
                if Path(card.path).resolve() == path.resolve():
                    return card.script_name or path.stem
            except Exception:  # pragma: no cover
                continue
        return path.stem


# ------------------------------------------------------------------ 日志组磁盘助手


def _new_log_id(now: float | None = None) -> str:
    """``<YYYY-MM-DDTHHMMSS>-<6-hex>``；hex 后缀避免同秒 rotate 冲突。"""
    ts = datetime.fromtimestamp(now if now is not None else time.time())
    stamp = ts.strftime("%Y-%m-%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _default_log_name(log_id: str) -> str:
    """默认显示名 = 从 log_id 里剥离随机后缀后的可读时间戳。"""
    # log_id 形如 2026-08-15T142315-abc123；剥掉最后 -abc123
    if "-" in log_id[10:]:
        return log_id.rsplit("-", 1)[0]
    return log_id  # pragma: no cover - 兜底


def _create_new_log(
    logs_root: Path, name: str | None = None, now: float | None = None
) -> WorkspaceLog:
    """在磁盘上创建一份新日志目录 + meta.json；返回未标记 is_active 的 WorkspaceLog。"""
    created_at = now if now is not None else time.time()
    lid = _new_log_id(created_at)
    root = logs_root / lid
    root.mkdir(parents=True, exist_ok=False)
    log = WorkspaceLog(
        id=lid,
        name=name or _default_log_name(lid),
        created_at=created_at,
        description="",
        pinned=False,
        root=root,
        run_count=0,
        is_active=False,
    )
    _write_log_meta(log)
    # 预建空 stream 文件，方便外部 tail -f
    (root / _STREAM_FILENAME).touch(exist_ok=True)
    return log


def _write_log_meta(log: WorkspaceLog) -> None:
    """把 ``meta.json`` 原子写入（tmp + replace，防止半写状态被下次启动读到）。"""
    meta = {
        "schema": _LOG_META_SCHEMA,
        "id": log.id,
        "name": log.name,
        "created_at": log.created_at,
        "description": log.description,
        "pinned": log.pinned,
    }
    path = log.root / "meta.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_log_meta(log_dir: Path) -> dict | None:
    """读一份 meta.json；缺失或损坏 → ``None``（调用方决定是否跳过该目录）。"""
    path = log_dir / "meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - 损坏 → 跳过
        _diag_logger.exception("failed to read %s", path)
        return None


def _write_active_log_pointer(logs_root: Path, log_id: str) -> None:
    """原子更新 ``_active.json``。"""
    pointer = logs_root / "_active.json"
    tmp = pointer.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"log_id": log_id}, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, pointer)


def _count_runs_in_stream(log_dir: Path) -> int:
    """数一下 stream 里有几个 run 段（即 ``workspace.run.begin`` 元行的条数）。

    只做子串预筛再解析，避免对每行都跑 ``json.loads``——stream 里绝大多数行是
    普通日志记录，不是元行。
    """
    stream = log_dir / _STREAM_FILENAME
    if not stream.exists():
        return 0
    needle = f'"{_EV_RUN_BEGIN}"'
    total = 0
    try:
        with open(stream, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if needle not in line:
                    continue
                try:
                    if json.loads(line).get("event") == _EV_RUN_BEGIN:
                        total += 1
                except (json.JSONDecodeError, AttributeError):  # pragma: no cover
                    continue
    except OSError:  # pragma: no cover
        return total
    return total


def _rebuild_runs_from_stream(log: WorkspaceLog) -> list[WorkspaceRun]:
    """扫一份 ``stream.ndjson``，按 begin/end 元行配对重建 run 摘要。

    - 逐行记录字节偏移，填入 ``stream_offset_begin`` / ``stream_offset_end``，
      让后续 ``iter_run_records`` 能直接 seek；
    - **孤立 begin**（进程崩溃 / 断电 / 强杀留下的残段）重建为
      ``status="interrupted"``，``ended_at`` 留空；
    - 半行 / 损坏行静默跳过。
    """
    stream = log.stream_path
    if not stream.exists():
        return []
    runs: list[WorkspaceRun] = []
    by_id: dict[str, int] = {}  # run_id -> runs 列表下标，供 end 元行回填
    begin_needle = f'"{_EV_RUN_BEGIN}"'
    end_needle = f'"{_EV_RUN_END}"'
    try:
        # 用二进制读才能拿到准确的字节偏移（文本模式的 tell() 不可按行累加）
        with open(stream, "rb") as fp:
            offset = 0
            for raw in fp:
                line_start = offset
                offset += len(raw)
                text = raw.decode("utf-8", errors="replace")
                if begin_needle not in text and end_needle not in text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:  # pragma: no cover
                    continue
                if not isinstance(data, dict):  # pragma: no cover
                    continue
                event = data.get("event")
                rid = data.get("run_id")
                if not isinstance(rid, str):  # pragma: no cover
                    continue
                if event == _EV_RUN_BEGIN:
                    by_id[rid] = len(runs)
                    runs.append(
                        WorkspaceRun(
                            id=rid,
                            script_path=Path(data.get("script_path", "")),
                            script_name=data.get("script_name") or rid,
                            # 没等到 end 就保持 interrupted
                            status=_STATUS_INTERRUPTED,
                            started_at=data.get("started_at"),
                            ended_at=None,
                            exception=None,
                            ndjson_path=stream,
                            snapshot=None,
                            log_id=data.get("log_id") or log.id,
                            stream_offset_begin=line_start,
                            stream_offset_end=None,
                        )
                    )
                elif event == _EV_RUN_END:
                    idx = by_id.get(rid)
                    if idx is None:  # 孤立 end（begin 被截断）→ 忽略
                        continue
                    runs[idx] = replace(
                        runs[idx],
                        status=data.get("status") or "succeeded",
                        ended_at=data.get("ended_at"),
                        exception=data.get("exception"),
                        stream_offset_end=offset,
                    )
    except OSError:  # pragma: no cover
        return runs
    return runs


def _iter_run_slice(
    stream: Path,
    run_id: str,
    *,
    offset_begin: int | None = None,
    offset_end: int | None = None,
) -> Iterator[dict]:
    """yield 某个 run 在 stream 里的记录区间（含首尾两条 workspace 元行）。

    已知字节区间时直接 seek 读那一段；否则从头顺序扫，靠 begin/end 元行定界。
    """
    try:
        with open(stream, "rb") as fp:
            if offset_begin is not None:
                fp.seek(offset_begin)
            inside = offset_begin is not None
            position = offset_begin or 0
            for raw in fp:
                position += len(raw)
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:  # pragma: no cover
                    continue
                if not isinstance(data, dict):  # pragma: no cover
                    continue
                event = data.get("event")
                if not inside:
                    # 还没进入目标区间：只找我们的 begin
                    if event == _EV_RUN_BEGIN and data.get("run_id") == run_id:
                        inside = True
                        yield data
                    continue
                yield data
                if event == _EV_RUN_END and data.get("run_id") == run_id:
                    return
                if offset_end is not None and position >= offset_end:
                    return
    except OSError:  # pragma: no cover
        return


def _rmtree_safe(root: Path) -> None:
    """带保护的目录递归删；只允许删 ``logs_root`` 子孙路径的目录。"""
    import shutil

    if not root.exists():
        return
    if not root.is_dir():
        raise RuntimeError(f"refuse to remove non-directory: {root}")
    # 要求路径包含 .redpymake 段，避免用户误传根目录被误删
    if ".redpymake" not in root.parts:
        raise RuntimeError(f"refuse to remove path outside .redpymake: {root}")
    shutil.rmtree(root)


# ------------------------------------------------------------------ 工具


def _record_to_dict(rec: SessionLogRecord) -> dict:
    """``SessionLogRecord`` → JSON-safe dict（与 NDJSON sink 输出结构对齐）。"""
    fields = rec.fields
    safe_fields: dict[str, Any] = {}
    if fields:
        for key, value in fields.items():
            try:
                json.dumps(value)
                safe_fields[key] = value
            except (TypeError, ValueError):
                safe_fields[key] = repr(value)
    return {
        "timestamp": rec.timestamp,
        "sequence": rec.sequence,
        "session_id": rec.session_id,
        "event": rec.event,
        "level": rec.level,
        "stream": rec.stream,
        "message": rec.message,
        "operation_id": rec.operation_id,
        "fields": safe_fields,
    }


def workspace(
    root: str | os.PathLike[str] = ".",
    *,
    logs_root: str | os.PathLike[str] | None = None,
    ndjson_dir: str | os.PathLike[str] | None = None,  # 兼容别名
    discovery_patterns: Sequence[str] | None = None,
    auto_close_sessions: bool = True,
    log_name: str | None = None,
    new_log_on_start: bool = False,
) -> Workspace:
    """构造一个 :class:`Workspace`（见 § CORE-11）。仅可作为上下文管理器使用。

    ``new_log_on_start=True`` 时进入即另起一份活跃日志（上一份为空则复用），
    ``redpymake serve`` 用它把每次启动隔成独立的一份"录制会话"。
    """
    return Workspace(
        root=root,
        logs_root=logs_root,
        ndjson_dir=ndjson_dir,
        discovery_patterns=discovery_patterns,
        auto_close_sessions=auto_close_sessions,
        log_name=log_name,
        new_log_on_start=new_log_on_start,
    )


__all__ = ["Workspace", "WorkspaceLog", "WorkspaceRun", "workspace"]
