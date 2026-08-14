"""``SshSession``：SSH 会话 (CORE-01)。

依赖：``paramiko``（optional extra ``[ssh]``）。

设计要点：

- 构造时立即连接；失败抛 ``SessionConnectionError``（不暴露 paramiko 原生异常）。
- 命令执行通过 ``SSHClient.exec_command`` + 逐行流式采集。
- 文件传输通过 SFTP；不支持 ``~`` 展开由 SFTP 侧处理，因此 ``home_dir`` 在连接
  成功后通过 ``pwd`` 命令探测。
"""

from __future__ import annotations

import contextvars
import os
import shlex
import threading
import time
from typing import Mapping, Sequence

from ._logs import LogBuffer
from ._path import ResourceStat
from ._session import Session
from .exceptions import (
    CommandTimeoutError,
    ResourceNotFoundError,
    SessionConnectionError,
    TransferError,
    UnsupportedOperationError,
)


def _require_paramiko():
    try:
        import paramiko  # type: ignore

        return paramiko
    except ImportError as exc:
        raise UnsupportedOperationError(
            "ssh support requires the 'paramiko' package; "
            "install with `pip install redpymake[ssh]`"
        ) from exc


class SshSession(Session):
    """基于 paramiko 的 SSH 会话。POSIX 路径风格。"""

    _is_local = False

    def __init__(
        self,
        host: str,
        *,
        user: str | None = None,
        port: int = 22,
        password: str | None = None,
        key_filename: str | os.PathLike[str] | None = None,
        default_cwd: str | None = None,
        connect_timeout: float = 15.0,
    ) -> None:
        paramiko = _require_paramiko()
        label = f"{user + '@' if user else ''}{host}:{port}"
        super().__init__(
            session_kind="ssh",
            session_label=label,
            default_cwd=default_cwd,
        )
        self.path_style = "posix"
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(
                hostname=host,
                port=port,
                username=user,
                password=password,
                key_filename=str(key_filename) if key_filename else None,
                timeout=connect_timeout,
                allow_agent=True,
                look_for_keys=True,
            )
        except Exception as exc:
            self._log_buffer.append(
                event="session_error",
                level="ERROR",
                stream="system",
                message=f"ssh connect failed: {exc}",
            )
            raise SessionConnectionError(
                f"failed to connect to {label}: {exc}",
                session=self,
                host=host,
                cause=exc,
            ) from exc

        self._log_buffer.append(
            event="session_open",
            level="INFO",
            stream="system",
            message=f"ssh session established: {label}",
        )

        # 探测 home_dir，便于 ~ 展开
        try:
            _, out = self._exec_capture("printf %s \"$HOME\"", timeout=5.0)
            if out.strip():
                self.home_dir = out.strip()
        except Exception:
            self.home_dir = None

        self._sftp_lock = threading.Lock()
        self._sftp = None  # 延迟打开

    # ------------------------------ 命令 ------------------------------

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
        if shell:
            cmd_str = argv[0]
        else:
            cmd_str = " ".join(shlex.quote(a) for a in argv)

        prefix_parts: list[str] = []
        if env:
            for k, v in env.items():
                prefix_parts.append(f"{shlex.quote(k)}={shlex.quote(v)}")
        prefix = " ".join(prefix_parts)

        if cwd:
            wrapped = f"cd {shlex.quote(cwd)} && {cmd_str}"
        else:
            wrapped = cmd_str
        if prefix:
            wrapped = f"{prefix} {wrapped}"

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                raise SessionConnectionError(
                    "ssh transport is inactive", session=self
                )
            chan = transport.open_session()
            chan.set_combine_stderr(False)
            if timeout is not None:
                chan.settimeout(timeout)
            chan.exec_command(wrapped)
        except SessionConnectionError:
            raise
        except Exception as exc:
            raise SessionConnectionError(
                f"failed to open ssh channel: {exc}", session=self, cause=exc
            ) from exc

        def _pump(recv_fn, sink: list[str], stream_name: str) -> None:
            buf = b""
            while True:
                try:
                    data = recv_fn(4096)
                except Exception:
                    data = b""
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    text = line_bytes.decode(encoding, errors="replace").rstrip("\r")
                    sink.append(text)
                    self._log_buffer.append(
                        event="command_output",
                        level="INFO",
                        stream=stream_name,
                        message=text,
                        operation_id=operation_id,
                    )
            if buf:
                text = buf.decode(encoding, errors="replace").rstrip("\r")
                sink.append(text)
                self._log_buffer.append(
                    event="command_output",
                    level="INFO",
                    stream=stream_name,
                    message=text,
                    operation_id=operation_id,
                )

        # 每个 pump 线程各自 copy 一份上下文（继承 ``logs.tag`` 等），
        # 避免两线程共享同一 Context 时的 "context already entered" 错误。
        ctx_out = contextvars.copy_context()
        ctx_err = contextvars.copy_context()
        t_out = threading.Thread(
            target=lambda: ctx_out.run(_pump, chan.recv, stdout_parts, "stdout"),
            daemon=True,
        )
        t_err = threading.Thread(
            target=lambda: ctx_err.run(_pump, chan.recv_stderr, stderr_parts, "stderr"),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        deadline = None if timeout is None else time.monotonic() + timeout
        while not chan.exit_status_ready():
            if deadline is not None and time.monotonic() > deadline:
                chan.close()
                t_out.join(timeout=1)
                t_err.join(timeout=1)
                raise CommandTimeoutError(
                    f"ssh command timed out after {timeout}s: {list(argv)!r}",
                    session=self,
                    command=argv,
                    timeout=timeout,
                    stdout="\n".join(stdout_parts),
                    stderr="\n".join(stderr_parts),
                )
            time.sleep(0.02)

        rc = chan.recv_exit_status()
        chan.close()
        t_out.join()
        t_err.join()
        return rc, "\n".join(stdout_parts), "\n".join(stderr_parts)

    def _exec_capture(self, cmd: str, *, timeout: float | None) -> tuple[int, str]:
        """内部工具：执行一段 shell 命令，返回 (rc, stdout)。不写日志。"""
        transport = self._client.get_transport()
        if transport is None:
            raise SessionConnectionError("ssh transport is inactive", session=self)
        chan = transport.open_session()
        if timeout is not None:
            chan.settimeout(timeout)
        chan.exec_command(cmd)
        out = b""
        while True:
            data = chan.recv(4096)
            if not data:
                break
            out += data
        rc = chan.recv_exit_status()
        chan.close()
        return rc, out.decode("utf-8", errors="replace")

    # ------------------------------ 资源 ------------------------------

    def _get_sftp(self):
        with self._sftp_lock:
            if self._sftp is None:
                try:
                    self._sftp = self._client.open_sftp()
                except Exception as exc:
                    raise SessionConnectionError(
                        f"failed to open sftp: {exc}", session=self, cause=exc
                    ) from exc
            return self._sftp

    def _resource_exists(self, path: str) -> bool:
        sftp = self._get_sftp()
        try:
            sftp.stat(path)
            return True
        except FileNotFoundError:
            return False
        except IOError:
            return False

    def _resource_is_file(self, path: str) -> bool:
        import stat as _s

        sftp = self._get_sftp()
        try:
            attrs = sftp.stat(path)
        except (FileNotFoundError, IOError):
            return False
        return _s.S_ISREG(attrs.st_mode or 0)

    def _resource_is_dir(self, path: str) -> bool:
        import stat as _s

        sftp = self._get_sftp()
        try:
            attrs = sftp.stat(path)
        except (FileNotFoundError, IOError):
            return False
        return _s.S_ISDIR(attrs.st_mode or 0)

    def _resource_stat(self, path: str) -> ResourceStat:
        import stat as _s

        sftp = self._get_sftp()
        try:
            attrs = sftp.stat(path)
        except (FileNotFoundError, IOError) as exc:
            raise ResourceNotFoundError(
                f"remote resource not found: {path}", path=path
            ) from exc
        return ResourceStat(
            size=int(attrs.st_size or 0),
            mtime=float(attrs.st_mtime or 0.0),
            is_dir=_s.S_ISDIR(attrs.st_mode or 0),
        )

    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None:
        sftp = self._get_sftp()
        try:
            sftp.remove(path)
        except FileNotFoundError:
            if not missing_ok:
                raise ResourceNotFoundError(
                    f"remote resource not found: {path}", path=path
                )
        except IOError as exc:
            # 可能是目录：尝试 rmdir
            try:
                sftp.rmdir(path)
            except IOError as exc2:
                if missing_ok:
                    return
                raise TransferError(
                    f"failed to remove remote resource: {exc2}", target=path, cause=exc2
                ) from exc2

    def _resource_mkdir(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        sftp = self._get_sftp()
        if parents:
            parts: list[str] = []
            for segment in path.split("/"):
                parts.append(segment)
                sub = "/".join(parts) if parts[0] else "/" + "/".join(parts[1:])
                if not sub:
                    continue
                try:
                    sftp.mkdir(sub)
                except IOError:
                    if not self._resource_is_dir(sub):
                        raise
        else:
            try:
                sftp.mkdir(path)
            except IOError:
                if exist_ok and self._resource_is_dir(path):
                    return
                raise

    # ------------------------------ 传输 ------------------------------

    def _do_copy_into(
        self, local_source: str, remote_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        sftp = self._get_sftp()
        if self._resource_exists(remote_target) and not overwrite:
            return 0, False
        try:
            attrs = sftp.put(local_source, remote_target)
        except FileNotFoundError as exc:
            raise ResourceNotFoundError(
                f"local source not found: {local_source}", path=local_source
            ) from exc
        return int(attrs.st_size or os.path.getsize(local_source)), True

    def _do_copy_from(
        self, remote_source: str, local_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        sftp = self._get_sftp()
        if os.path.exists(local_target) and not overwrite:
            return 0, False
        parent = os.path.dirname(local_target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            sftp.get(remote_source, local_target)
        except FileNotFoundError as exc:
            raise ResourceNotFoundError(
                f"remote source not found: {remote_source}", path=remote_source
            ) from exc
        return os.path.getsize(local_target), True

    def _do_copy_within(
        self, source: str, target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        # 通过远端 shell 命令实现 in-session 复制
        cmd = f"cp -f -- {shlex.quote(source)} {shlex.quote(target)}"
        if not overwrite:
            cmd = f"[ -e {shlex.quote(target)} ] || {cmd}"
        rc, out = self._exec_capture(cmd, timeout=timeout)
        if rc != 0:
            raise TransferError(
                f"remote copy failed (rc={rc}): {out}", source=source, target=target
            )
        try:
            size = int(self._resource_stat(target).size)
        except ResourceNotFoundError:
            size = 0
        return size, True

    # ------------------------------ 关闭 ------------------------------

    def _close_impl(self) -> None:
        with self._sftp_lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None
        try:
            self._client.close()
        except Exception:
            pass


__all__ = ["SshSession"]
