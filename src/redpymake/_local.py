"""``LocalSession``：本地进程会话 (CORE-01/02/03/04)。

- ``run()`` 使用 ``subprocess.Popen`` 逐行采集 stdout/stderr 并写入日志缓冲。
- 传输实现直接文件系统操作，支持 push/pull/copy 三种入口。
- ``_resource_*`` 直接使用 ``os`` / ``pathlib``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from ._logs import LogBuffer
from ._path import ResourceStat
from ._session import Session
from .exceptions import (
    CommandTimeoutError,
    ResourceNotFoundError,
    TransferError,
    UnsupportedOperationError,
)


class LocalSession(Session):
    """本地进程会话。构造后即可用，无需网络连接。"""

    _is_local = True

    def __init__(self, *, default_cwd: str | None = None) -> None:
        # 路径风格随宿主 OS。
        style = "windows" if os.name == "nt" else "posix"
        label = "local"
        super().__init__(
            session_kind="local",
            session_label=label,
            default_cwd=default_cwd or os.getcwd(),
        )
        self.path_style = style
        self.home_dir = str(Path.home())
        self._log_buffer.append(
            event="session_open",
            level="INFO",
            stream="system",
            message=f"local session ready (cwd={self.default_cwd})",
        )

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
        exec_env: dict[str, str] | None
        if env is None:
            exec_env = None
        else:
            exec_env = dict(os.environ)
            exec_env.update(env)

        popen_kwargs: dict = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=exec_env,
            bufsize=1,
            text=True,
            encoding=encoding,
            errors="replace",
        )

        if shell:
            proc = subprocess.Popen(argv[0], shell=True, **popen_kwargs)
        else:
            proc = subprocess.Popen(list(argv), shell=False, **popen_kwargs)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def _pump(stream, sink, stream_name: str) -> None:
            if stream is None:
                return
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    text = line.rstrip("\n").rstrip("\r")
                    sink.append(text)
                    self._log_buffer.append(
                        event="command_output",
                        level="INFO",
                        stream=stream_name,
                        message=text,
                        operation_id=operation_id,
                    )
            finally:
                stream.close()

        t_out = threading.Thread(
            target=_pump,
            args=(proc.stdout, stdout_parts, "stdout"),
            name=f"local-stdout-{operation_id}",
            daemon=True,
        )
        t_err = threading.Thread(
            target=_pump,
            args=(proc.stderr, stderr_parts, "stderr"),
            name=f"local-stderr-{operation_id}",
            daemon=True,
        )
        t_out.start()
        t_err.start()

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            # 让 pump 线程结束
            t_out.join(timeout=1)
            t_err.join(timeout=1)
            stdout = "\n".join(stdout_parts)
            stderr = "\n".join(stderr_parts)
            raise CommandTimeoutError(
                f"local command timed out after {timeout}s: {list(argv)!r}",
                session=self,
                command=argv,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
            )

        t_out.join()
        t_err.join()
        stdout = "\n".join(stdout_parts)
        stderr = "\n".join(stderr_parts)
        return returncode, stdout, stderr

    # ------------------------------ 资源 ------------------------------

    def _resource_exists(self, path: str) -> bool:
        return os.path.exists(path)

    def _resource_is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def _resource_is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def _resource_stat(self, path: str) -> ResourceStat:
        try:
            st = os.stat(path)
        except FileNotFoundError as exc:
            raise ResourceNotFoundError(
                f"local resource not found: {path}", path=path
            ) from exc
        return ResourceStat(size=st.st_size, mtime=st.st_mtime, is_dir=os.path.isdir(path))

    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except FileNotFoundError:
            if not missing_ok:
                raise ResourceNotFoundError(
                    f"local resource not found: {path}", path=path
                )

    def _resource_mkdir(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if parents:
            os.makedirs(path, exist_ok=exist_ok)
        else:
            try:
                os.mkdir(path)
            except FileExistsError:
                if not exist_ok:
                    raise

    # ------------------------------ 传输 ------------------------------

    def _do_copy_within(
        self, source: str, target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        if not os.path.exists(source):
            raise ResourceNotFoundError(f"local source not found: {source}", path=source)
        if os.path.isdir(source):
            raise UnsupportedOperationError(
                "directory copy is not supported in first version"
            )
        if os.path.exists(target) and not overwrite:
            return 0, False
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copyfile(source, target)
        size = os.path.getsize(target)
        return size, True

    def _do_copy_into(
        self, local_source: str, remote_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        # 对本地会话来说 into/within 是一回事
        return self._do_copy_within(
            local_source, remote_target, overwrite=overwrite, timeout=timeout
        )

    def _do_copy_from(
        self, remote_source: str, local_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        return self._do_copy_within(
            remote_source, local_target, overwrite=overwrite, timeout=timeout
        )


__all__ = ["LocalSession"]
