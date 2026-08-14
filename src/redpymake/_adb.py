"""``AdbSession``：基于 ``adb`` CLI 的 Android 设备会话 (CORE-01)。

要求本机 ``adb`` 命令可用。构造时通过 ``adb -s <serial> get-state`` 立即校验。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Mapping, Sequence

from ._path import ResourceStat
from ._session import Session
from .exceptions import (
    CommandTimeoutError,
    ResourceNotFoundError,
    SessionConnectionError,
    TransferError,
    UnsupportedOperationError,
)


class AdbSession(Session):
    """通过 adb CLI 与设备通信。"""

    _is_local = False

    def __init__(
        self,
        serial: str | None = None,
        *,
        adb_path: str | None = None,
        default_cwd: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        adb_exe = adb_path or shutil.which("adb")
        if not adb_exe:
            raise SessionConnectionError(
                "adb executable not found in PATH; set adb_path=... or install adb"
            )
        label = f"adb:{serial or '(default)'}"
        super().__init__(
            session_kind="adb",
            session_label=label,
            default_cwd=default_cwd or "/",
        )
        self.path_style = "posix"
        self._adb = adb_exe
        self._serial = serial
        # 立即连接校验
        try:
            rc, out, err = self._run_adb(["get-state"], timeout=connect_timeout)
        except Exception as exc:
            raise SessionConnectionError(
                f"failed to invoke adb: {exc}", cause=exc
            ) from exc
        if rc != 0 or "device" not in out.strip():
            raise SessionConnectionError(
                f"adb device unavailable (rc={rc}, state={out.strip()!r}, err={err.strip()!r})",
                host=serial,
            )
        self._log_buffer.append(
            event="session_open",
            level="INFO",
            stream="system",
            message=f"adb session ready ({label})",
        )

    # ------------------------------ 内部 adb 调用 ------------------------------

    def _adb_argv(self, extra: Sequence[str]) -> list[str]:
        cmd = [self._adb]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(extra)
        return cmd

    def _run_adb(
        self,
        extra: Sequence[str],
        *,
        timeout: float | None,
        stdin: bytes | None = None,
    ) -> tuple[int, str, str]:
        proc = subprocess.run(
            self._adb_argv(extra),
            input=stdin,
            capture_output=True,
            timeout=timeout,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
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

        # 使用 shell -c "..." 把返回码带出：adb shell 的 rc 不可靠。
        # 用魔术标记从 stdout 抽取 rc。
        marker = f"__RPM_EXIT_{operation_id}__"
        remote_cmd = f"{wrapped}; printf '\\n{marker}%d\\n' $?"

        popen = subprocess.Popen(
            self._adb_argv(["shell", remote_cmd]),
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
        rc_holder: dict[str, int | None] = {"rc": None}

        def _pump_stdout() -> None:
            assert popen.stdout is not None
            for line in iter(popen.stdout.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n").rstrip("\r")
                if text.startswith(marker):
                    try:
                        rc_holder["rc"] = int(text[len(marker):])
                    except ValueError:
                        rc_holder["rc"] = None
                    continue
                stdout_parts.append(text)
                self._log_buffer.append(
                    event="command_output",
                    level="INFO",
                    stream="stdout",
                    message=text,
                    operation_id=operation_id,
                )
            popen.stdout.close()

        def _pump_stderr() -> None:
            assert popen.stderr is not None
            for line in iter(popen.stderr.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n").rstrip("\r")
                stderr_parts.append(text)
                self._log_buffer.append(
                    event="command_output",
                    level="INFO",
                    stream="stderr",
                    message=text,
                    operation_id=operation_id,
                )
            popen.stderr.close()

        t1 = threading.Thread(target=_pump_stdout, daemon=True)
        t2 = threading.Thread(target=_pump_stderr, daemon=True)
        t1.start()
        t2.start()

        try:
            popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            popen.kill()
            t1.join(timeout=1)
            t2.join(timeout=1)
            raise CommandTimeoutError(
                f"adb command timed out after {timeout}s: {list(argv)!r}",
                session=self,
                command=argv,
                timeout=timeout,
                stdout="\n".join(stdout_parts),
                stderr="\n".join(stderr_parts),
            )

        t1.join()
        t2.join()
        rc = rc_holder["rc"] if rc_holder["rc"] is not None else popen.returncode
        return rc, "\n".join(stdout_parts), "\n".join(stderr_parts)

    # ------------------------------ 资源 ------------------------------

    def _shell_probe(self, cmd: str, *, timeout: float | None = 10.0) -> tuple[int, str]:
        rc, out, _err = self._run_adb(["shell", cmd], timeout=timeout)
        return rc, out

    def _resource_exists(self, path: str) -> bool:
        rc, _ = self._shell_probe(
            f"[ -e {shlex.quote(path)} ] && echo yes || echo no"
        )
        return "yes" in _.strip()

    def _resource_is_file(self, path: str) -> bool:
        rc, out = self._shell_probe(
            f"[ -f {shlex.quote(path)} ] && echo yes || echo no"
        )
        return "yes" in out.strip()

    def _resource_is_dir(self, path: str) -> bool:
        rc, out = self._shell_probe(
            f"[ -d {shlex.quote(path)} ] && echo yes || echo no"
        )
        return "yes" in out.strip()

    def _resource_stat(self, path: str) -> ResourceStat:
        # `stat -c '%s %Y %F'` 在多数 Android 上可用
        rc, out = self._shell_probe(f"stat -c '%s %Y %F' {shlex.quote(path)}")
        text = out.strip()
        if rc != 0 or not text:
            raise ResourceNotFoundError(
                f"adb resource not found: {path}", path=path
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
        rc, out = self._shell_probe(f"rm -rf {shlex.quote(path)}")
        if rc != 0 and not missing_ok:
            raise TransferError(f"adb remove failed: {out}", target=path)

    def _resource_mkdir(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        flag = "-p " if parents or exist_ok else ""
        rc, out = self._shell_probe(f"mkdir {flag}{shlex.quote(path)}")
        if rc != 0 and not exist_ok:
            raise TransferError(f"adb mkdir failed: {out}", target=path)

    # ------------------------------ 传输 ------------------------------

    def _do_copy_into(
        self, local_source: str, remote_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        if self._resource_exists(remote_target) and not overwrite:
            return 0, False
        rc, out, err = self._run_adb(
            ["push", local_source, remote_target], timeout=timeout
        )
        if rc != 0:
            raise TransferError(
                f"adb push failed: {err or out}", source=local_source, target=remote_target
            )
        try:
            size = os.path.getsize(local_source)
        except OSError:
            size = 0
        return size, True

    def _do_copy_from(
        self, remote_source: str, local_target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        if os.path.exists(local_target) and not overwrite:
            return 0, False
        parent = os.path.dirname(local_target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        rc, out, err = self._run_adb(
            ["pull", remote_source, local_target], timeout=timeout
        )
        if rc != 0:
            raise TransferError(
                f"adb pull failed: {err or out}", source=remote_source, target=local_target
            )
        try:
            size = os.path.getsize(local_target)
        except OSError:
            size = 0
        return size, True

    def _do_copy_within(
        self, source: str, target: str, *, overwrite: bool, timeout: float | None
    ) -> tuple[int, bool]:
        cmd = f"cp -f {shlex.quote(source)} {shlex.quote(target)}"
        if not overwrite:
            cmd = f"[ -e {shlex.quote(target)} ] || {cmd}"
        rc, out = self._shell_probe(cmd, timeout=timeout)
        if rc != 0:
            raise TransferError(
                f"adb in-device copy failed: {out}", source=source, target=target
            )
        try:
            size = self._resource_stat(target).size
        except ResourceNotFoundError:
            size = 0
        return size, True


__all__ = ["AdbSession"]
