"""``WslSession``：基于 ``wsl.exe`` 的 Windows Subsystem for Linux 会话 (CORE-01)。

设计要点：

- WSL 被视作"本地已运行的 Linux 用户态"：构造时**只校验 ``wsl.exe`` 可执行**，
  不做 distro 级探测或 ``$HOME`` 探测（作为 CORE-01 "构造时立即连接"要求的
  显式例外），因此构造几乎零延迟。distro 未安装 / 冷启动失败会在首次 ``run()``
  时以 ``CommandError`` / 非零退出码呈现。
- 命令执行：走 ``wsl.exe [-d D] [-u U] -- sh -c "<wrapped>"``；``<wrapped>`` 内
  注入 ``cd <cwd> && KEY=value ... cmd``，与 ``AdbSession`` 同构。WSL 会正确
  回传 rc，无需 marker trick。
- 资源接口：POSIX 侧探测（``test -e/-f/-d``, ``stat -c``, ``rm -rf``, ``mkdir``）。
- 传输：Windows 本地路径转换为 ``/mnt/<drive>/...``，全部走 ``wsl -e cp``。

不引入新依赖：``wsl.exe`` 是 Windows 组件；非 Windows 环境下 ``shutil.which``
返回 ``None``，构造直接抛 ``SessionConnectionError``。
"""

from __future__ import annotations

import contextvars
import ntpath
import os
import shlex
import shutil
import subprocess
import threading
from typing import Mapping, Sequence

from ._path import ResourceStat
from ._session import Session
from .exceptions import (
    CommandTimeoutError,
    ResourceNotFoundError,
    SessionConnectionError,
    TransferError,
)


class WslSession(Session):
    """通过 ``wsl.exe`` 与 Windows Subsystem for Linux 通信。POSIX 路径风格。"""

    _is_local = False

    def __init__(
        self,
        distribution: str | None = None,
        *,
        user: str | None = None,
        wsl_path: str | None = None,
        default_cwd: str | None = None,
    ) -> None:
        wsl_exe = wsl_path or shutil.which("wsl")
        if not wsl_exe:
            raise SessionConnectionError(
                "wsl executable not found in PATH; set wsl_path=... or install "
                "Windows Subsystem for Linux"
            )
        label = f"wsl:{distribution or '(default)'}"
        if user:
            label = f"{label}#{user}"
        super().__init__(
            session_kind="wsl",
            session_label=label,
            default_cwd=default_cwd or "/",
        )
        self.path_style = "posix"
        # 不做 $HOME 探测，避免为一次可选的 ~ 展开付出冷启动代价。
        self.home_dir = None
        self._wsl = wsl_exe
        self._distribution = distribution
        self._user = user
        self._log_buffer.append(
            event="session_open",
            level="INFO",
            stream="system",
            message=f"wsl session ready ({label})",
        )

    # ------------------------------ 内部 wsl 调用 ------------------------------

    def _wsl_argv(self, extra: Sequence[str]) -> list[str]:
        cmd = [self._wsl]
        if self._distribution:
            cmd.extend(["-d", self._distribution])
        if self._user:
            cmd.extend(["-u", self._user])
        cmd.extend(extra)
        return cmd

    def _run_wsl(
        self,
        extra: Sequence[str],
        *,
        timeout: float | None,
        stdin: bytes | None = None,
    ) -> tuple[int, str, str]:
        """一次性调用 wsl.exe，捕获全部 stdout/stderr（不做流式日志）。"""
        proc = subprocess.run(
            self._wsl_argv(extra),
            input=stdin,
            capture_output=True,
            timeout=timeout,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def _wsl_shell(
        self, cmd: str, *, timeout: float | None = 10.0
    ) -> tuple[int, str]:
        """在 WSL 内跑一段 shell 命令，返回 ``(rc, stdout)``；不写日志。"""
        rc, out, _err = self._run_wsl(["--", "sh", "-c", cmd], timeout=timeout)
        return rc, out

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

        env_prefix = ""
        if env:
            env_prefix = " ".join(
                f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items()
            ) + " "

        if cwd:
            wrapped = f"cd {shlex.quote(cwd)} && {env_prefix}{cmd_str}"
        else:
            wrapped = f"{env_prefix}{cmd_str}"

        popen = subprocess.Popen(
            self._wsl_argv(["--", "sh", "-c", wrapped]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            encoding=encoding,
            errors="replace",
        )

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def _pump(stream, sink: list[str], stream_name: str) -> None:
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

        ctx_out = contextvars.copy_context()
        ctx_err = contextvars.copy_context()
        t_out = threading.Thread(
            target=lambda: ctx_out.run(_pump, popen.stdout, stdout_parts, "stdout"),
            name=f"wsl-stdout-{operation_id}",
            daemon=True,
        )
        t_err = threading.Thread(
            target=lambda: ctx_err.run(_pump, popen.stderr, stderr_parts, "stderr"),
            name=f"wsl-stderr-{operation_id}",
            daemon=True,
        )
        t_out.start()
        t_err.start()

        try:
            returncode = popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            popen.kill()
            t_out.join(timeout=1)
            t_err.join(timeout=1)
            raise CommandTimeoutError(
                f"wsl command timed out after {timeout}s: {list(argv)!r}",
                session=self,
                command=argv,
                timeout=timeout,
                stdout="\n".join(stdout_parts),
                stderr="\n".join(stderr_parts),
            )

        t_out.join()
        t_err.join()
        return returncode, "\n".join(stdout_parts), "\n".join(stderr_parts)

    # ------------------------------ 资源 ------------------------------

    def _resource_exists(self, path: str) -> bool:
        rc, out = self._wsl_shell(
            f"[ -e {shlex.quote(path)} ] && echo yes || echo no"
        )
        return "yes" in out.strip()

    def _resource_is_file(self, path: str) -> bool:
        rc, out = self._wsl_shell(
            f"[ -f {shlex.quote(path)} ] && echo yes || echo no"
        )
        return "yes" in out.strip()

    def _resource_is_dir(self, path: str) -> bool:
        rc, out = self._wsl_shell(
            f"[ -d {shlex.quote(path)} ] && echo yes || echo no"
        )
        return "yes" in out.strip()

    def _resource_stat(self, path: str) -> ResourceStat:
        rc, out = self._wsl_shell(f"stat -c '%s %Y %F' {shlex.quote(path)}")
        text = out.strip()
        if rc != 0 or not text:
            raise ResourceNotFoundError(
                f"wsl resource not found: {path}", path=path
            )
        try:
            size_s, mtime_s, kind = text.split(None, 2)
            return ResourceStat(
                size=int(size_s),
                mtime=float(mtime_s),
                is_dir="directory" in kind.lower(),
            )
        except Exception as exc:
            raise ResourceNotFoundError(
                f"unexpected stat output: {text!r}", path=path
            ) from exc

    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None:
        rc, out = self._wsl_shell(f"rm -rf {shlex.quote(path)}")
        if rc != 0 and not missing_ok:
            raise TransferError(f"wsl remove failed: {out}", target=path)

    def _resource_mkdir(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        flag = "-p " if parents or exist_ok else ""
        rc, out = self._wsl_shell(f"mkdir {flag}{shlex.quote(path)}")
        if rc != 0 and not exist_ok:
            raise TransferError(f"wsl mkdir failed: {out}", target=path)

    # ------------------------------ 传输 ------------------------------

    def _do_copy_into(
        self,
        local_source: str,
        remote_target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        if self._resource_exists(remote_target) and not overwrite:
            return 0, False
        if not os.path.exists(local_source):
            raise ResourceNotFoundError(
                f"local source not found: {local_source}", path=local_source
            )
        mnt_src = _win_to_mnt(local_source)
        rc, out = self._wsl_shell(
            f"cp -f {shlex.quote(mnt_src)} {shlex.quote(remote_target)}",
            timeout=timeout,
        )
        if rc != 0:
            raise TransferError(
                f"wsl push failed: {out}", source=local_source, target=remote_target
            )
        try:
            size = os.path.getsize(local_source)
        except OSError:
            size = 0
        return size, True

    def _do_copy_from(
        self,
        remote_source: str,
        local_target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        if os.path.exists(local_target) and not overwrite:
            return 0, False
        parent = os.path.dirname(local_target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        mnt_tgt = _win_to_mnt(local_target)
        rc, out = self._wsl_shell(
            f"cp -f {shlex.quote(remote_source)} {shlex.quote(mnt_tgt)}",
            timeout=timeout,
        )
        if rc != 0:
            raise TransferError(
                f"wsl pull failed: {out}", source=remote_source, target=local_target
            )
        try:
            size = os.path.getsize(local_target)
        except OSError:
            size = 0
        return size, True

    def _do_copy_within(
        self,
        source: str,
        target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        cmd = f"cp -f {shlex.quote(source)} {shlex.quote(target)}"
        if not overwrite:
            cmd = f"[ -e {shlex.quote(target)} ] || {cmd}"
        rc, out = self._wsl_shell(cmd, timeout=timeout)
        if rc != 0:
            raise TransferError(
                f"wsl in-distro copy failed: {out}", source=source, target=target
            )
        try:
            size = int(self._resource_stat(target).size)
        except ResourceNotFoundError:
            size = 0
        return size, True

    # ------------------------------ 关闭 ------------------------------

    def _close_impl(self) -> None:
        # 无持久通道；每次调用都独立起 wsl.exe 子进程。
        return None


# --------------------------- 路径转换 ---------------------------


def _win_to_mnt(path: str) -> str:
    """把 Windows 路径转换为 WSL 里的 ``/mnt/<drive>/...`` 形式。

    - 绝对路径 ``C:\\a\\b`` → ``/mnt/c/a/b``。
    - UNC 路径（``\\\\server\\share``）不支持转换，原样返回，交由 wsl 侧处理。
    - 已经是 posix 风格（``/tmp/x``）直接返回。
    - 相对路径以调用方 cwd 为基准，展开成绝对路径后再转换。
    """
    if not path:
        return path
    # 已经是 posix 绝对路径（本地传输前一般会经 resolve_against 变成绝对，但对
    # LocalSession 而言绝对路径可能是 posix 形式，例如 msys/git-bash 下）。
    if path.startswith("/"):
        return path

    # 归一化 + 补上绝对路径（相对路径基于当前进程 cwd）
    abs_path = ntpath.abspath(path)
    drive, tail = ntpath.splitdrive(abs_path)
    if not drive or not drive.endswith(":"):
        # UNC 或其它不好转的形式：交给 wsl 侧原样处理
        return abs_path.replace("\\", "/")
    letter = drive[0].lower()
    posix_tail = tail.replace("\\", "/")
    if not posix_tail.startswith("/"):
        posix_tail = "/" + posix_tail
    return f"/mnt/{letter}{posix_tail}"


__all__ = ["WslSession"]
