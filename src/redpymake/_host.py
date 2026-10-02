"""OS-aware execution and temporary staging on an SSH host. No remote install."""
from __future__ import annotations

import base64
import contextlib
import ntpath
import os
import re
import shlex
import subprocess
from typing import Any, Iterator, Mapping, Sequence

from ._process import _Deadline, _capture
from .exceptions import SessionConnectionError, TransferError


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str) -> str:
    encoded = _ps_encoded(script)
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def _ps_encoded(script: str) -> str:
    prelude = ("$ProgressPreference='SilentlyContinue';"
               "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);")
    return base64.b64encode((prelude + script).encode("utf-16le")).decode("ascii")


def _posix_command(argv: Sequence[str], *, shell: bool = False,
                   cwd: str | None = None, env: Mapping[str, str] | None = None) -> str:
    command = argv[0] if shell else shlex.join(argv)
    statements = []
    if cwd:
        statements.append("cd " + shlex.quote(cwd))
    if env:
        for name, value in env.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError(f"invalid environment variable name: {name!r}")
            statements.append("export " + name + "=" + shlex.quote(value))
    statements.append(command)
    return " && ".join(statements)


def _windows_command(argv: Sequence[str], *, shell: bool = False,
                     cwd: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Use .NET binary pipes, not PowerShell's object/text native pipeline.

    ProcessStartInfo.Arguments is a Windows command line; list2cmdline protects
    native argv (including embedded quotes) independently of PowerShell quoting.
    """
    if shell:
        argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", _ps_encoded(argv[0])]
    relay = ("using System.IO;using System.Threading.Tasks;public static class RpmPipe{"
             "public static async Task Copy(Stream a,Stream b,bool close){try{"
             "byte[] v=new byte[32768];int n;while((n=await a.ReadAsync(v,0,v.Length))>0){"
             "await b.WriteAsync(v,0,n);await b.FlushAsync();}}finally{if(close)b.Close();}}}")
    script = ["$ErrorActionPreference='Stop'", "$ProgressPreference='SilentlyContinue'",
              "Add-Type -TypeDefinition " + _ps_quote(relay),
              "$p=New-Object System.Diagnostics.Process",
              "$p.StartInfo.UseShellExecute=$false",
              "$p.StartInfo.RedirectStandardInput=$true",
              "$p.StartInfo.RedirectStandardOutput=$true",
              "$p.StartInfo.RedirectStandardError=$true",
              "$p.StartInfo.FileName=" + _ps_quote(argv[0]),
              "$p.StartInfo.Arguments=" + _ps_quote(subprocess.list2cmdline(argv[1:]))]
    if cwd:
        script.append("$p.StartInfo.WorkingDirectory=" + _ps_quote(cwd))
    for name, value in (env or {}).items():
        script.append("$p.StartInfo.EnvironmentVariables[" + _ps_quote(name) + "]=" + _ps_quote(value))
    script.extend([
        "[void]$p.Start()",
        "$o=[RpmPipe]::Copy($p.StandardOutput.BaseStream,[Console]::OpenStandardOutput(),$false)",
        "$e=[RpmPipe]::Copy($p.StandardError.BaseStream,[Console]::OpenStandardError(),$false)",
        "$i=[RpmPipe]::Copy([Console]::OpenStandardInput(),$p.StandardInput.BaseStream,$true)",
        "$p.WaitForExit()", "[void]$o.GetAwaiter().GetResult()", "[void]$e.GetAwaiter().GetResult()",
        "exit $p.ExitCode",
    ])
    return _powershell(";".join(script))


class _SshHost:
    def __init__(self, session: Any, platform: str) -> None:
        self.session = session
        self.platform = platform

    def build(self, argv: Sequence[str], **kwargs: Any) -> str:
        return (_windows_command if self.platform == "windows" else _posix_command)(argv, **kwargs)

    def spawn(self, argv: Sequence[str], *, timeout: float | None = None):
        return self.session._open_process(self.build(argv), timeout=timeout)

    def capture(self, argv: Sequence[str], *, timeout: float | None = None,
                session: Any = None, encoding: str = "utf-8",
                operation_id: str | None = None, line_filter: Any = None) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        process = self.spawn(argv, timeout=deadline.remaining())
        return _capture(process, session=session or self.session, command=argv,
                        timeout=timeout, deadline=deadline, encoding=encoding, operation_id=operation_id,
                        line_filter=line_filter)

    def shell(self, script: str, *, timeout: float | None = 10) -> tuple[int, str, str]:
        if self.platform == "windows":
            command = _powershell("$ErrorActionPreference='Stop';" + script)
        else:
            command = "sh -c " + shlex.quote(script)
        return self.session._raw_capture(command, timeout=timeout)

    def executable(self, name: str, *, timeout: float = 10) -> str:
        script = ("(Get-Command -CommandType Application -Name " + _ps_quote(name) +
                  " -ErrorAction Stop).Source" if self.platform == "windows" else
                  "command -v " + shlex.quote(name))
        rc, out, err = self.shell(script, timeout=timeout)
        if rc or not out.strip():
            raise SessionConnectionError(f"{self.session.label}: executable {name!r} unavailable: {err or out}",
                                         session=self.session)
        return out.strip().splitlines()[-1]

    @contextlib.contextmanager
    def temporary_directory(self, deadline: _Deadline) -> Iterator[str]:
        if self.platform == "windows":
            script = ("$d=Join-Path ([IO.Path]::GetTempPath()) ('redpymake-'+[Guid]::NewGuid().ToString('N'));"
                      "[void](New-Item -ItemType Directory -Path $d -ErrorAction Stop);[Console]::Write($d)")
        else:
            script = 'mktemp -d "${TMPDIR:-/tmp}/redpymake-XXXXXXXXXX"'
        rc, out, err = self.shell(script, timeout=deadline.remaining())
        directory = out.strip()
        flavor = ntpath if self.platform == "windows" else __import__("posixpath")
        if rc or not flavor.isabs(directory) or not flavor.basename(directory).startswith("redpymake-"):
            raise TransferError(f"failed to create remote temporary directory: {err or out}",
                                returncode=rc, stdout=out, stderr=err)
        primary: BaseException | None = None
        try:
            yield directory
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                cleanup = ("Remove-Item -LiteralPath " + _ps_quote(directory) + " -Recurse -Force -ErrorAction Stop"
                           if self.platform == "windows" else "rm -rf -- " + shlex.quote(directory))
                # Cleanup gets a separate bounded grace period after operation timeout.
                code, stdout, stderr = self.shell(cleanup, timeout=5)
                if code:
                    raise TransferError(f"remote temporary directory remains at {directory}: {stderr or stdout}",
                                        target=directory, returncode=code, stdout=stdout, stderr=stderr)
            except Exception as cleanup_error:
                if primary is None:
                    raise TransferError(f"remote cleanup failed at {directory}: {cleanup_error}",
                                        target=directory, cause=cleanup_error) from cleanup_error
                setattr(primary, "cleanup_error", cleanup_error)
                setattr(primary, "temporary_path", directory)
                if not self.session._log_buffer.closed:
                    self.session._log_buffer.append(event="transfer_error", level="ERROR", stream="system",
                        message=f"remote cleanup failed at {directory}: {cleanup_error}")

    def join(self, directory: str, name: str) -> str:
        return (ntpath.join if self.platform == "windows" else __import__("posixpath").join)(directory, name)

    def put(self, local: str, remote: str, deadline: _Deadline) -> None:
        self.session._do_copy_into(local, remote, overwrite=True, timeout=deadline.remaining())

    def get(self, remote: str, local: str, deadline: _Deadline) -> None:
        self.session._do_copy_from(remote, local, overwrite=True, timeout=deadline.remaining())

    def wsl_path(self, path: str, argv_prefix: Sequence[str], deadline: _Deadline) -> str:
        rc, out, err = self.capture([*argv_prefix, "--", "wslpath", "-u", path],
                                    timeout=deadline.remaining())
        if rc or not out.startswith("/"):
            raise TransferError(f"cannot map host staging path into WSL: {err or out}",
                                returncode=rc, stdout=out, stderr=err)
        return out.strip()


def _detect_host(session: Any, platform: str, timeout: float) -> tuple[str, str]:
    if platform not in {"auto", "posix", "windows"}:
        raise ValueError("platform must be 'auto', 'posix', or 'windows'")
    deadline = _Deadline.start(timeout)
    diagnostics = []
    if platform in {"auto", "posix"}:
        command = "sh -c " + shlex.quote('printf "__RPM_POSIX__\\n"; uname -s; printf "%s\\n" "$HOME"')
        rc, out, err = session._raw_capture(command, timeout=deadline.remaining())
        lines = out.splitlines()
        if rc == 0 and len(lines) >= 3 and lines[-3] == "__RPM_POSIX__" and lines[-1].startswith("/"):
            if not any(word in lines[-2].upper() for word in ("MINGW", "MSYS", "CYGWIN")):
                return "posix", lines[-1]
        diagnostics.append(f"POSIX rc={rc}: {err or out}")
    if platform in {"auto", "windows"}:
        command = _powershell("$ErrorActionPreference='Stop';[Console]::WriteLine('__RPM_WINDOWS__');"
                              "[Console]::WriteLine([Environment]::GetFolderPath('UserProfile'))")
        rc, out, err = session._raw_capture(command, timeout=deadline.remaining())
        lines = out.splitlines()
        if rc == 0 and len(lines) >= 2 and lines[-2] == "__RPM_WINDOWS__" and ntpath.isabs(lines[-1]):
            return "windows", lines[-1]
        diagnostics.append(f"Windows rc={rc}: {err or out}")
    raise SessionConnectionError("cannot identify SSH execution environment: " + "; ".join(diagnostics),
                                 session=session)
