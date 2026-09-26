"""统一会话抽象基类 (CORE-01/02/03/04/06 基座)。

具体平台会话（``LocalSession`` / ``SshSession`` / ``AdbSession`` /
``SerialSession``）继承 ``Session`` 并实现下列钩子：

- 命令执行：``_execute_command`` — 供 ``run()`` 调用，返回 ``(returncode, stdout,
  stderr, duration)``；命令输出应逐行 append 到 ``self._log_buffer``。
- 传输：``_do_copy_into`` / ``_do_copy_from`` — 分别在"当前会话是目标 / 源"时
  搬运字节。默认实现回退到 ``UnsupportedOperationError``；本地会话覆写为直接
  文件系统操作，其他平台按能力覆写。
- 资源：``_resource_*`` — 由 ``ResourcePath`` 转发的探测/删除/创建接口。
- 关闭：``_close_impl`` — 释放底层连接资源。

日志：所有会话共享 ``SessionLogs`` + ``LogBuffer``；``at()`` 视图共享同一缓冲。
"""

from __future__ import annotations

import itertools
import os
import shlex
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Mapping, Pattern, Sequence

from ._command import CommandResult
from ._logs import LogBuffer, LogCursor, LogMatch, SessionLogs, normalize_wait_patterns
from ._path import ResourcePath, ResourceStat, resolve_against
from ._transfer import TransferResult
from .exceptions import (
    CommandError,
    CommandTimeoutError,
    ResourceNotFoundError,
    SessionClosedError,
    TransferError,
    UnsupportedOperationError,
)

_session_counter = itertools.count(1)


def _new_operation_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class LazyHome:
    """``Session._home_dir`` 的内部哨兵：家目录尚未探测。

    与 ``None``（探测过，但没有可用家目录）区分。读 ``home_dir`` 不会触发探测；
    展开 ``~`` / ``~/...`` 时才加载。
    """

    def __repr__(self) -> str:
        return "<LazyHome>"


LAZY_HOME = LazyHome()


class Session(ABC):
    """所有平台会话的抽象基类。

    子类需实现的钩子在类底部列出。
    """

    #: 会话使用的路径风格：``"posix"`` 或 ``"windows"``。
    path_style: str = "posix"
    #: 会话默认工作目录；未指定 ``at()`` 且 ``run(cwd=None)`` 时使用。
    default_cwd: str | None = None
    #: 家目录缓存。``LAZY_HOME`` 表示尚未探测。
    _home_dir: "str | None | LazyHome" = LAZY_HOME

    def __init__(
        self,
        *,
        session_kind: str,
        session_label: str,
        log_buffer: LogBuffer | None = None,
        parent: "Session | None" = None,
        default_cwd: str | None = None,
    ) -> None:
        # ``parent`` 存在时（``at()`` 视图）：共享 buffer / 底层连接
        self._parent = parent
        self._session_kind = session_kind
        self._session_label = session_label
        self._session_id = (
            parent._session_id
            if parent is not None
            else f"{session_kind}:{session_label}#{next(_session_counter)}"
        )
        self._log_buffer = (
            parent._log_buffer if parent is not None else (log_buffer or LogBuffer(self._session_id))
        )
        self._logs = SessionLogs(self._log_buffer)
        self._closed = False
        self._close_lock = threading.Lock()
        # None：wait() 从调用当下的缓冲末尾开始。run() 会把它拨到该命令开始前。
        # 串口在子类里设为 0，以便第一次 wait 看到开机日志。
        self._wait_origin_seq: int | None = None
        if default_cwd is not None:
            self.default_cwd = default_cwd

        # CORE-09：仅 root Session 感知 ContextVar；进入 ``with rpm.script(...):``
        # 后构造的会话自动 attach 到当前活跃的 ``ScriptRun``。视图与 root 共享
        # ``LogBuffer``，已被 root 的订阅覆盖，无需重复登记。
        if parent is None:
            from ._script import _current_script

            run = _current_script.get()
            if run is not None:
                run.attach(self)

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def kind(self) -> str:
        return self._session_kind

    @property
    def label(self) -> str:
        return self._session_label

    @property
    def is_view(self) -> bool:
        return self._parent is not None

    @property
    def root(self) -> "Session":
        s: Session = self
        while s._parent is not None:
            s = s._parent
        return s

    @property
    def logs(self) -> SessionLogs:
        return self._logs

    @property
    def home_dir(self) -> str | None:
        """已探测到的家目录。尚未探测时为 ``None``，且不会因此去探测。"""
        value = self.root._home_dir
        if value is LAZY_HOME:
            return None
        return value

    @home_dir.setter
    def home_dir(self, value: str | None) -> None:
        self.root._home_dir = value

    def _ensure_home_dir(self) -> str | None:
        """展开 ``~`` 时调用。``LAZY_HOME`` 才探测；结果缓存在 root 上。"""
        root = self.root
        if root._home_dir is LAZY_HOME:
            probed = root._probe_home_dir()
            root._home_dir = probed or None
        return root._home_dir

    def _probe_home_dir(self) -> str | None:
        """子类覆写。默认没有可探测的家目录。"""
        return None

    @property
    def closed(self) -> bool:
        return self.root._closed

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{type(self).__name__} {self._session_label!r} id={self._session_id}>"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """幂等关闭：仅 root 触发底层资源释放，视图直接返回。"""
        if self._parent is not None:
            return
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._close_impl()
        finally:
            self._log_buffer.append(
                event="session_closed",
                level="INFO",
                stream="system",
                message=f"session closed: {self._session_label}",
            )
            # 关闭日志缓冲：拒绝新写入，但已收集记录仍可读取。
            self._log_buffer.close()

    def _check_alive(self) -> None:
        if self.closed:
            raise SessionClosedError(
                f"session '{self._session_label}' is closed",
                session=self,
            )

    # ------------------------------------------------------------------
    # 工作目录视图
    # ------------------------------------------------------------------

    def at(self, path: str | os.PathLike[str]) -> "Session":
        """返回绑定新工作目录的视图；共享底层连接与日志。"""
        self._check_alive()
        new_cwd = resolve_against(self, path, cwd=self.default_cwd)
        return _SessionView(self.root, self, new_cwd)

    # ------------------------------------------------------------------
    # 路径工厂
    # ------------------------------------------------------------------

    def path(self, raw: str | os.PathLike[str]) -> ResourcePath:
        resolved = resolve_against(self, raw, cwd=self.default_cwd)
        return ResourcePath(self, os.fspath(raw), resolved)

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------

    def run(
        self,
        command: str | bytes,
        *args: str,
        shell: bool = False,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
        encoding: str = "utf-8",
        log_command: bool = True,
    ) -> CommandResult:
        """执行一条命令。语义详见 ``doc/core-lib-requirements.md`` § CORE-02。"""

        self._check_alive()

        argv: tuple[str | bytes, ...]
        if isinstance(command, bytes):
            if self.root.kind != "serial":
                raise TypeError("bytes command is only supported on serial sessions")
            if args:
                raise TypeError("bytes command cannot take extra positional args")
            if shell:
                raise TypeError("bytes command is incompatible with shell=True")
            argv = (command,)
        elif shell:
            if args:
                raise TypeError(
                    "shell=True expects a single command string; got extra positional args"
                )
            argv = (command,)
        else:
            if not isinstance(command, str):
                raise TypeError("command must be a string")
            argv = (command,) + tuple(args)
            for i, a in enumerate(argv):
                if not isinstance(a, str):
                    raise TypeError(
                        f"run() positional args must be str; args[{i}] is {type(a).__name__}"
                    )

        effective_cwd = cwd or self.default_cwd
        op_id = _new_operation_id("cmd")
        # run 前保存日志游标 / RX 偏移，确保后续 wait() 不漏这次命令的输出
        cursor_before = self._log_buffer.cursor()
        rx_before = self._rx_cursor()
        self._note_wait_origin(cursor_before.sequence)

        if log_command:
            display = _format_command(argv, shell=shell)
            self._log_buffer.append(
                event="command_start",
                level="INFO",
                stream="system",
                message=f"$ {display}"
                + (f"  (cwd={effective_cwd})" if effective_cwd else ""),
                operation_id=op_id,
                fields={
                    "argv": list(argv),
                    "shell": shell,
                    "cwd": effective_cwd,
                    "env": (list(env.keys()) if env else None),
                    "timeout": timeout,
                },
            )

        started = time.monotonic()
        try:
            returncode, stdout, stderr = self._execute_command(
                argv,
                shell=shell,
                cwd=effective_cwd,
                env=env,
                timeout=timeout,
                encoding=encoding,
                operation_id=op_id,
            )
        except CommandTimeoutError:
            elapsed = time.monotonic() - started
            self._log_buffer.append(
                event="command_end",
                level="ERROR",
                stream="system",
                message=f"timeout after {timeout}s",
                operation_id=op_id,
                fields={"returncode": None, "duration": elapsed, "timeout": timeout},
            )
            raise
        except CommandError:
            raise
        except Exception as exc:  # 归一化为框架异常
            elapsed = time.monotonic() - started
            self._log_buffer.append(
                event="command_error",
                level="ERROR",
                stream="system",
                message=f"command raised {type(exc).__name__}: {exc}",
                operation_id=op_id,
                fields={"elapsed": elapsed},
            )
            raise

        duration = time.monotonic() - started

        self._log_buffer.append(
            event="command_end",
            level="INFO" if returncode == 0 else "WARNING",
            stream="system",
            message=f"exit={returncode} in {duration:.3f}s",
            operation_id=op_id,
            fields={"returncode": returncode, "duration": duration},
        )

        result = CommandResult(
            command=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            session=self,
            _log_cursor_before=cursor_before,
            _rx_cursor_before=rx_before,
        )
        if check and returncode != 0:
            raise CommandError(
                f"command exited with code {returncode}: {argv!r}",
                session=self,
                command=argv,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return result

    # ------------------------------------------------------------------
    # 等待日志
    # ------------------------------------------------------------------

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
        _pats, is_bytes = normalize_wait_patterns(pattern)
        if is_bytes:
            if channel is not None or multiline:
                raise TypeError("channel and multiline do not apply to bytes wait")
            if since is not None:
                raise TypeError("since=LogCursor does not apply to bytes wait")
            return self._wait_bytes(
                _pats,
                timeout=timeout,
                command_result=command_result,
                start_offset=rx_since,
                original_pattern=pattern,
            )
        effective_since = since if since is not None else self._default_wait_since()
        match = self._log_buffer.wait(
            pattern,
            timeout=timeout,
            channel=channel,
            since=effective_since,
            command_result=command_result,
            multiline=multiline,
            session=self,
        )
        if match.record is not None:
            self._advance_wait_origin(match.record.sequence + 1)
        return match

    def _note_wait_origin(self, sequence: int) -> None:
        """把后续 ``wait()`` 的默认起点单调拨到 ``sequence``。"""
        root = self.root
        current = root._wait_origin_seq
        if current is None or sequence > current:
            root._wait_origin_seq = sequence

    def _default_wait_since(self) -> LogCursor | None:
        root = self.root
        if root._wait_origin_seq is None:
            return None
        return LogCursor(self._log_buffer.session_id, root._wait_origin_seq)

    def _advance_wait_origin(self, sequence: int) -> None:
        self._note_wait_origin(sequence)

    def _rx_cursor(self) -> int | None:
        """串口 RX 单调偏移；非串口为 ``None``。视图转发给 root。"""
        if self._parent is not None:
            return self.root._rx_cursor()
        return None

    def _wait_bytes(
        self,
        patterns: Sequence[Any],
        timeout: float,
        *,
        command_result: CommandResult | None,
        start_offset: int | None,
        original_pattern: Any,
    ) -> LogMatch:
        if self._parent is not None:
            return self.root._wait_bytes(
                patterns,
                timeout,
                command_result=command_result,
                start_offset=start_offset,
                original_pattern=original_pattern,
            )
        raise UnsupportedOperationError(
            "bytes wait is only supported on serial sessions"
        )

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    def push(
        self,
        source: "ResourcePath | str | os.PathLike[str]",
        target: "ResourcePath | str | os.PathLike[str]",
        *,
        overwrite: bool = True,
        timeout: float | None = None,
    ) -> TransferResult:
        """从其他环境传入当前会话。

        - 若两端会话相同：与 ``rpm.copy(source, target)`` 走同一套传输。
        - 若 ``source`` 是本地/远端路径，``target`` 必须属于当前会话；否则报错。
        """
        self._check_alive()
        src = _as_path(self, source, allow_local=True)
        tgt = _as_path(self, target, allow_local=False)
        if tgt.session.root is not self.root:
            raise TransferError(
                "push() target must belong to the calling session",
                source=source,
                target=target,
            )
        return _perform_transfer(src, tgt, overwrite=overwrite, timeout=timeout, kind="push")

    def pull(
        self,
        source: "ResourcePath | str | os.PathLike[str]",
        target: "ResourcePath | str | os.PathLike[str]",
        *,
        overwrite: bool = True,
        timeout: float | None = None,
    ) -> TransferResult:
        """从当前会话传出到其他环境。"""
        self._check_alive()
        src = _as_path(self, source, allow_local=False)
        tgt = _as_path(self, target, allow_local=True)
        if src.session.root is not self.root:
            raise TransferError(
                "pull() source must belong to the calling session",
                source=source,
                target=target,
            )
        return _perform_transfer(src, tgt, overwrite=overwrite, timeout=timeout, kind="pull")

    # ------------------------------------------------------------------
    # 传输 hook（供子类覆写）
    # ------------------------------------------------------------------

    def _do_copy_into(
        self,
        local_source: str,
        remote_target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        """将 *本地* 文件写入当前会话（远端）。

        返回 ``(bytes_transferred, actually_transferred)``。默认抛
        ``UnsupportedOperationError``；本地会话与实现了 SFTP 的会话应覆写。
        """
        raise UnsupportedOperationError(
            f"{type(self).__name__} does not support copy into (push)"
        )

    def _do_copy_from(
        self,
        remote_source: str,
        local_target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        """将 *当前会话（远端）* 上的文件写入本地。"""
        raise UnsupportedOperationError(
            f"{type(self).__name__} does not support copy from (pull)"
        )

    def _do_copy_within(
        self,
        source: str,
        target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        """当前会话内部复制。默认不支持。"""
        raise UnsupportedOperationError(
            f"{type(self).__name__} does not support within-session copy"
        )

    # ------------------------------------------------------------------
    # 资源查询 hook（供 ResourcePath 转发）
    # ------------------------------------------------------------------

    @abstractmethod
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
    ) -> tuple[int, str, str]: ...

    @abstractmethod
    def _resource_exists(self, path: str) -> bool: ...

    @abstractmethod
    def _resource_is_file(self, path: str) -> bool: ...

    @abstractmethod
    def _resource_is_dir(self, path: str) -> bool: ...

    @abstractmethod
    def _resource_stat(self, path: str) -> ResourceStat: ...

    @abstractmethod
    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None: ...

    @abstractmethod
    def _resource_mkdir(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None: ...

    def _close_impl(self) -> None:
        """默认无资源；有连接的子类覆写。"""


class _SessionView(Session):
    """``at()`` 返回的视图。共享底层 root 的连接与日志缓冲。"""

    def __init__(self, root: Session, parent: Session, new_cwd: str) -> None:
        # 直接从 parent 派生
        super().__init__(
            session_kind=root._session_kind,
            session_label=root._session_label,
            parent=parent,
            default_cwd=new_cwd,
        )
        # 强制 path_style 沿用 root。home_dir 懒加载，读的时候走 root，不在这里拷贝。
        self.path_style = root.path_style

    # 所有 IO 委托给 root，实现共用连接

    def _execute_command(self, argv, *, shell, cwd, env, timeout, encoding, operation_id):
        return self.root._execute_command(
            argv,
            shell=shell,
            cwd=cwd,
            env=env,
            timeout=timeout,
            encoding=encoding,
            operation_id=operation_id,
        )

    def _resource_exists(self, path: str) -> bool:
        return self.root._resource_exists(path)

    def _resource_is_file(self, path: str) -> bool:
        return self.root._resource_is_file(path)

    def _resource_is_dir(self, path: str) -> bool:
        return self.root._resource_is_dir(path)

    def _resource_stat(self, path: str) -> ResourceStat:
        return self.root._resource_stat(path)

    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None:
        self.root._resource_remove(path, missing_ok=missing_ok)

    def _resource_mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        self.root._resource_mkdir(path, parents=parents, exist_ok=exist_ok)

    def _do_copy_into(self, local_source, remote_target, *, overwrite, timeout):
        return self.root._do_copy_into(
            local_source, remote_target, overwrite=overwrite, timeout=timeout
        )

    def _do_copy_from(self, remote_source, local_target, *, overwrite, timeout):
        return self.root._do_copy_from(
            remote_source, local_target, overwrite=overwrite, timeout=timeout
        )

    def _do_copy_within(self, source, target, *, overwrite, timeout):
        return self.root._do_copy_within(
            source, target, overwrite=overwrite, timeout=timeout
        )


# ------------------------------ 工具 ------------------------------


def _format_command(argv: Sequence[str | bytes], *, shell: bool) -> str:
    if shell:
        a0 = argv[0]
        return a0 if isinstance(a0, str) else repr(a0)
    parts: list[str] = []
    for a in argv:
        if isinstance(a, bytes):
            parts.append(repr(a))
        else:
            try:
                parts.append(shlex.quote(a))
            except Exception:  # pragma: no cover - shlex 极少失败
                parts.append(a)
    return " ".join(parts)


def _as_path(
    session: Session,
    spec: "ResourcePath | str | os.PathLike[str]",
    *,
    allow_local: bool,
) -> ResourcePath:
    """将 ``str``/``PathLike``/``ResourcePath`` 归一为 ``ResourcePath``。

    ``allow_local``：当 ``spec`` 是纯字符串时，是否允许将其解释为本地路径。
    True：非本地调用方绑到进程级默认 local 会话；本地调用方绑到自己。
    False：绑到调用会话。
    """
    if isinstance(spec, ResourcePath):
        return spec
    if allow_local and not getattr(session.root, "_is_local", False):
        from ._factory import _default_local

        return _default_local().path(spec)
    return session.path(spec)


def _endpoint_roots(src: ResourcePath, tgt: ResourcePath) -> list[Session]:
    """源、目标的 root 会话。同一会话只出现一次。"""
    roots: list[Session] = []
    for sess in (src.session, tgt.session):
        root = sess.root
        if all(root is not seen for seen in roots):
            roots.append(root)
    return roots


def _log_transfer(roots: list[Session], **kwargs: Any) -> None:
    for root in roots:
        root._log_buffer.append(**kwargs)


def copy(
    source: "ResourcePath | str | os.PathLike[str]",
    target: "ResourcePath | str | os.PathLike[str]",
    *,
    overwrite: bool = True,
    timeout: float | None = None,
) -> TransferResult:
    """在源路径和目标路径之间复制。

    裸字符串与 ``os.PathLike`` 绑到进程级默认 local 会话，不跟随其它
    ``rpm.local(default_cwd=...)`` 的工作目录。日志写在源会话和目标会话上；
    两端相同则只记一次。
    """
    from ._factory import _active_workspace_or_none, _default_local

    ws = _active_workspace_or_none()
    if ws is not None:
        ws._check_stop_requested("copy")
    src = source if isinstance(source, ResourcePath) else _default_local().path(source)
    tgt = target if isinstance(target, ResourcePath) else _default_local().path(target)
    return _perform_transfer(src, tgt, overwrite=overwrite, timeout=timeout, kind="copy")


def _perform_transfer(
    src: ResourcePath,
    tgt: ResourcePath,
    *,
    overwrite: bool,
    timeout: float | None,
    kind: str,
) -> TransferResult:
    roots = _endpoint_roots(src, tgt)
    for root in roots:
        root._check_alive()
    op_id = _new_operation_id("xfer")
    start_fields = {
        "kind": kind,
        "source": str(src),
        "source_session": src.session.session_id,
        "target": str(tgt),
        "target_session": tgt.session.session_id,
    }
    _log_transfer(
        roots,
        event="transfer_start",
        level="INFO",
        stream="system",
        message=f"{kind}: {src} -> {tgt}",
        operation_id=op_id,
        fields=start_fields,
    )
    start = time.monotonic()
    try:
        bytes_transferred, transferred = _dispatch_copy(
            src, tgt, overwrite=overwrite, timeout=timeout
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        _log_transfer(
            roots,
            event="transfer_error",
            level="ERROR",
            stream="system",
            message=f"{kind} failed: {exc}",
            operation_id=op_id,
            fields={"elapsed": elapsed, "error": str(exc)},
        )
        if isinstance(exc, TransferError):
            raise
        raise TransferError(
            f"{kind} failed: {exc}", source=str(src), target=str(tgt), cause=exc
        ) from exc

    duration = time.monotonic() - start
    _log_transfer(
        roots,
        event="transfer_end",
        level="INFO",
        stream="system",
        message=(
            f"{kind} done: {bytes_transferred} bytes in {duration:.3f}s"
            if transferred
            else f"{kind} skipped: target exists and overwrite=False"
        ),
        operation_id=op_id,
        fields={
            "transferred": transferred,
            "bytes": bytes_transferred,
            "duration": duration,
        },
    )
    return TransferResult(
        source=src,
        target=tgt,
        transferred=transferred,
        bytes_transferred=bytes_transferred,
        duration=duration,
    )


def _dispatch_copy(
    src: ResourcePath,
    tgt: ResourcePath,
    *,
    overwrite: bool,
    timeout: float | None,
) -> tuple[int, bool]:
    src_root = src.session.root
    tgt_root = tgt.session.root

    # 若源与目标在同一会话内：走内部 copy
    if src_root is tgt_root:
        return src_root._do_copy_within(
            src.path, tgt.path, overwrite=overwrite, timeout=timeout
        )

    # 若目标是本地：让源会话导出到本地路径
    if getattr(tgt_root, "_is_local", False):
        return src_root._do_copy_from(
            src.path, tgt.path, overwrite=overwrite, timeout=timeout
        )

    # 若源是本地：让目标会话导入本地路径
    if getattr(src_root, "_is_local", False):
        return tgt_root._do_copy_into(
            src.path, tgt.path, overwrite=overwrite, timeout=timeout
        )

    # 远端到远端：走本地临时文件中转
    return _copy_via_local_tmp(src, tgt, overwrite=overwrite, timeout=timeout)


def _copy_via_local_tmp(
    src: ResourcePath,
    tgt: ResourcePath,
    *,
    overwrite: bool,
    timeout: float | None,
) -> tuple[int, bool]:
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="redpymake-xfer-", suffix=".bin")
    os.close(fd)
    try:
        bytes_out, transferred = src.session.root._do_copy_from(
            src.path, tmp, overwrite=True, timeout=timeout
        )
        if not transferred:
            return bytes_out, False
        return tgt.session.root._do_copy_into(
            tmp, tgt.path, overwrite=overwrite, timeout=timeout
        )
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


__all__ = ["Session"]
