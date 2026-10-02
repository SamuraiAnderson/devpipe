"""``SshSession``：SSH 会话 (CORE-01)。

依赖：``paramiko``（optional extra ``[ssh]``）。

设计要点：

- 构造时立即连接；失败抛 ``SessionConnectionError``（不暴露 paramiko 原生异常）。
- 命令执行通过 ``SSHClient.exec_command`` + 逐行流式采集。
- 文件传输通过 SFTP；SFTP 不展开 ``~``。连接成功后探测 ``$HOME``，
  失败则关闭连接并抛出，不把会话留在「家目录未知」的状态。
"""

from __future__ import annotations

import contextlib
import errno
import os
import shlex
import threading
import time
from typing import Mapping, Sequence

from ._logs import LogBuffer
from ._path import ResourceStat
from ._session import Session
from .exceptions import (
    CommandError,
    CommandTimeoutError,
    ResourceNotFoundError,
    ResourceError,
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
    """SSH command host with detected POSIX/Windows execution semantics."""

    _is_local = False

    def __init__(self, host: str, *, user: str | None = None, port: int = 22,
                 password: str | None = None,
                 key_filename: str | os.PathLike[str] | None = None,
                 default_cwd: str | None = None, connect_timeout: float = 15.0,
                 platform: str = "auto") -> None:
        from ._host import _detect_host, _SshHost
        if platform not in {"auto", "posix", "windows"}:
            raise ValueError("platform must be 'auto', 'posix', or 'windows'")
        paramiko = _require_paramiko()
        label = f"{user + '@' if user else ''}{host}:{port}"
        super().__init__(session_kind="ssh", session_label=label, default_cwd=default_cwd)
        self._sftp_lock = threading.RLock()
        self._sftp = None
        self._process_lock = threading.Lock()
        self._processes = set()
        self._monitor_stop = threading.Event()
        self._monitor = None
        self._connect_timeout = connect_timeout
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(hostname=host, port=port, username=user, password=password,
                key_filename=str(key_filename) if key_filename else None,
                timeout=connect_timeout, auth_timeout=connect_timeout, banner_timeout=connect_timeout,
                allow_agent=True, look_for_keys=True)
            detected, home = _detect_host(self, platform, connect_timeout)
            self.path_style = "windows" if detected == "windows" else "posix"
            self._home_dir = home
            self._host_backend = _SshHost(self, detected)
            transport = self._client.get_transport()
            transport.set_keepalive(15)
            self._log_buffer.append(event="session_open", level="INFO", stream="system",
                message=f"ssh session established: {label} ({detected})",
                fields={"platform": detected})
            self._monitor = threading.Thread(target=self._watch_transport, daemon=True,
                                             name=f"rpm-ssh-watch-{host}")
            self._monitor.start()
        except Exception as exc:
            try:
                self.close()
            except Exception as cleanup:
                setattr(exc, "cleanup_error", cleanup)
            raise SessionConnectionError(f"failed to connect to {label}: {exc}",
                                         session=self, host=host, cause=exc) from exc

    def _watch_transport(self) -> None:
        next_probe = time.monotonic() + 15
        while not self._monitor_stop.wait(0.2):
            try:
                transport = self._client.get_transport()
                if transport is None or not transport.is_active():
                    raise OSError("SSH transport disconnected")
                if time.monotonic() >= next_probe:
                    script = ("[Console]::Write('__RPM_ALIVE__')" if self.path_style == "windows"
                              else "printf __RPM_ALIVE__")
                    rc, out, err = self._host_backend.shell(script, timeout=min(5, self._connect_timeout))
                    if rc or out.strip() != "__RPM_ALIVE__":
                        raise OSError(f"SSH liveness probe failed: {err or out}")
                    next_probe = time.monotonic() + 15
            except Exception as exc:
                if not self._monitor_stop.is_set():
                    self._fail(exc)
                return

    def _open_process(self, command: str, *, timeout: float | None):
        from ._process import _Deadline, _SshProcess
        self._check_alive()
        deadline = _Deadline.start(timeout)
        channel = None
        timer = None
        expired = threading.Event()
        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                raise OSError("SSH transport is inactive")
            channel = transport.open_session(timeout=deadline.remaining())
            channel.set_combine_stderr(False)
            channel.settimeout(0.2)

            def cancel_start():
                expired.set()
                channel.close()

            left = deadline.remaining()
            if left is not None:
                timer = threading.Timer(left, cancel_start)
                timer.daemon = True
                timer.start()
            channel.exec_command(command)
            if expired.is_set():
                raise TimeoutError("SSH exec request timed out")
            process = _SshProcess(channel, self)
            with self._process_lock:
                self._check_alive()
                self._processes.add(process)
            return process
        except Exception as exc:
            if channel is not None:
                channel.close()
            if isinstance(exc, (TimeoutError, CommandTimeoutError)) or expired.is_set():
                raise CommandTimeoutError("SSH process start timed out; remote state unconfirmed",
                                          session=self, command=(command,), timeout=timeout) from exc
            raise SessionConnectionError(f"failed to open SSH channel: {exc}",
                                         session=self, cause=exc, outcome="unknown") from exc
        finally:
            if timer is not None:
                timer.cancel()

    def _release_process(self, process) -> None:
        with self._process_lock:
            self._processes.discard(process)

    def _raw_capture(self, command: str, *, timeout: float | None) -> tuple[int, str, str]:
        from ._process import _Deadline, _capture
        deadline = _Deadline.start(timeout)
        process = self._open_process(command, timeout=deadline.remaining())
        return _capture(process, session=self, command=(command,), timeout=timeout, deadline=deadline)

    def _execute_command(self, argv, *, shell, cwd, env, timeout, encoding, operation_id):
        from ._process import _Deadline, _capture
        deadline = _Deadline.start(timeout)
        command = self._host_backend.build(argv, shell=shell, cwd=cwd, env=env)
        process = self._open_process(command, timeout=deadline.remaining())
        return _capture(process, session=self, command=argv, timeout=timeout, deadline=deadline,
                        encoding=encoding, operation_id=operation_id)

    def _exec_capture(self, cmd: str, *, timeout: float | None) -> tuple[int, str]:
        rc, out, err = self._raw_capture(cmd, timeout=timeout)
        return rc, out if rc == 0 else (err or out)

    def _probe_home_dir(self) -> str:
        return self._home_dir

    # ------------------------------ 资源 ------------------------------

    def _get_sftp(self, timeout: float | None = None):
        self._check_alive()
        with self._sftp_lock:
            if self._sftp is None:
                channel = None
                timer = None
                try:
                    transport = self._client.get_transport()
                    channel = transport.open_session(timeout=timeout or self._connect_timeout)
                    channel.settimeout(timeout or self._connect_timeout)
                    timer = threading.Timer(timeout or self._connect_timeout, channel.close)
                    timer.daemon = True
                    timer.start()
                    channel.invoke_subsystem("sftp")
                    self._sftp = _require_paramiko().SFTPClient(channel)
                except Exception as exc:
                    if channel is not None:
                        channel.close()
                    raise SessionConnectionError(
                        f"failed to open sftp: {exc}", session=self, cause=exc
                    ) from exc
                finally:
                    if timer is not None:
                        timer.cancel()
            return self._sftp

    @contextlib.contextmanager
    def _sftp_operation(self, deadline):
        left = deadline.remaining()
        acquired = self._sftp_lock.acquire() if left is None else self._sftp_lock.acquire(timeout=left)
        if not acquired:
            raise CommandTimeoutError("SFTP lock deadline exceeded", timeout=deadline.timeout)
        timer = None
        expired = threading.Event()
        sftp = None
        try:
            sftp = self._get_sftp(deadline.remaining())
            channel = sftp.get_channel()
            left = deadline.remaining()
            channel.settimeout(left)
            if left is not None:
                def cancel():
                    expired.set()
                    channel.close()
                timer = threading.Timer(left, cancel)
                timer.daemon = True
                timer.start()
            yield sftp
            deadline.remaining()
        finally:
            if timer is not None:
                timer.cancel()
            if expired.is_set():
                self._sftp = None
            self._sftp_lock.release()

    def _sftp_path(self, path: str) -> str:
        return path.replace("\\", "/") if self.path_style == "windows" else path

    def _stat_attrs(self, path: str):
        from ._process import _Deadline
        try:
            with self._sftp_operation(_Deadline.start(self._connect_timeout)) as sftp:
                return sftp.stat(self._sftp_path(path))
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise ResourceNotFoundError(f"remote resource not found: {path}", path=path) from exc
            raise ResourceError(f"cannot stat {path}: {exc}", path=path) from exc
        except (SessionConnectionError, CommandTimeoutError):
            raise
        except Exception as exc:
            raise ResourceError(f"cannot stat {path}: {exc}", path=path) from exc

    def _resource_exists(self, path: str) -> bool:
        try:
            self._stat_attrs(path)
            return True
        except ResourceNotFoundError:
            return False

    def _resource_is_file(self, path: str) -> bool:
        import stat as _s

        try:
            attrs = self._stat_attrs(path)
        except ResourceNotFoundError:
            return False
        return _s.S_ISREG(attrs.st_mode or 0)

    def _resource_is_dir(self, path: str) -> bool:
        import stat as _s

        try:
            attrs = self._stat_attrs(path)
        except ResourceNotFoundError:
            return False
        return _s.S_ISDIR(attrs.st_mode or 0)

    def _resource_stat(self, path: str) -> ResourceStat:
        import stat as _s

        attrs = self._stat_attrs(path)
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
        from ._process import _Deadline
        deadline = _Deadline.start(timeout)
        try:
            with self._sftp_operation(deadline) as sftp:
                path = self._sftp_path(remote_target)
                if not overwrite:
                    try:
                        sftp.stat(path)
                    except OSError as exc:
                        if exc.errno != errno.ENOENT:
                            raise
                    else:
                        return 0, False
                attrs = sftp.put(local_source, path, callback=lambda *_: deadline.remaining())
        except Exception as exc:
            raise TransferError(f"SFTP put failed: {exc}", source=local_source,
                                target=remote_target, cause=exc) from exc
        return int(attrs.st_size or os.path.getsize(local_source)), True

    def _do_copy_from(
        self, remote_source: str, local_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        from ._process import _Deadline
        deadline = _Deadline.start(timeout)
        if os.path.exists(local_target) and not overwrite:
            return 0, False
        parent = os.path.dirname(local_target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with self._sftp_operation(deadline) as sftp:
                sftp.get(self._sftp_path(remote_source), local_target,
                         callback=lambda *_: deadline.remaining())
        except Exception as exc:
            raise TransferError(f"SFTP get failed: {exc}", source=remote_source,
                                target=local_target, cause=exc) from exc
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
        self._monitor_stop.set()
        failures = []
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            try:
                process.close()
            except Exception as exc:
                failures.append(exc)
        with self._sftp_lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception as exc:
                    failures.append(exc)
                self._sftp = None
        try:
            self._client.close()
        except Exception as exc:
            failures.append(exc)
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=2)
        if failures:
            raise SessionConnectionError(f"SSH cleanup failed: {failures[0]}",
                                         session=self, cause=failures[0]) from failures[0]


__all__ = ["SshSession"]
