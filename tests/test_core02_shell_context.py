"""Specification mapping:
§CORE-02/shell-context → cwd failure, script scope, env inheritance, literal argv.
§CORE-02/background → explicit shell wait and resulting exit status.

Transport boundaries are replaced; every command is executed by a real POSIX sh.
On Windows this uses Git's sh if installed, without starting a WSL distribution.
"""
from contextlib import ExitStack
import shlex
import shutil
import subprocess

import pytest
import redpymake as rpm

from ._helpers.chain_host import install_host, Reply


@pytest.fixture(params=["wsl", "adb", "ssh", "ssh-wsl", "ssh-adb"])
def shell_endpoint(request, monkeypatch, tmp_path):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("requires a POSIX sh (Git for Windows includes one)")
    directory = tmp_path / "中文 work's dir"
    directory.mkdir()
    (directory / "marker").write_text("in-cwd", encoding="utf-8")
    cwd = subprocess.run(
        [sh, "-c", "cd " + shlex.quote(directory.as_posix()) + " && pwd"],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=5,
    ).stdout.strip()
    native_popen = subprocess.Popen

    def launch(argv, *args, **kwargs):
        if argv[0] in {"rpm-test-wsl", r"C:\Windows\System32\wsl.exe"}:
            argv = [sh, "-c", argv[-1]]
        elif argv[0] == "rpm-test-adb":
            argv = [sh, "-c", "printf device" if argv[-1] == "get-state" else argv[-1]]
        return native_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)

    def capture(argv, *, timeout=None, encoding="utf-8", line_filter=None, **kwargs):
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding=encoding, timeout=timeout)
        out, err = result.stdout, result.stderr
        if line_filter:
            out = "\n".join(line for line in out.splitlines() if line_filter(0, line))
            err = "\n".join(line for line in err.splitlines() if line_filter(1, line))
        return result.returncode, out, err

    with ExitStack() as stack:
        kind = request.param
        if kind.startswith("ssh"):
            wire = install_host(monkeypatch, "windows" if kind == "ssh-wsl" else "posix")
            host = stack.enter_context(rpm.ssh("test-host"))
            if kind == "ssh":
                def respond(command):
                    result = subprocess.run([sh, "-c", command], capture_output=True, timeout=5)
                    return Reply(result.stdout, result.stderr, result.returncode)
                wire.handler = respond
                session = host
            else:
                session = stack.enter_context(
                    host.wsl(wsl_path="rpm-test-wsl") if kind == "ssh-wsl"
                    else host.adb(adb_path="rpm-test-adb")
                )
                monkeypatch.setattr(session._host, "capture", capture)
        else:
            session = stack.enter_context(
                rpm.wsl(wsl_path="rpm-test-wsl") if kind == "wsl"
                else rpm.adb(adb_path="rpm-test-adb")
            )
        yield session, cwd


@pytest.mark.parametrize("script", [
    "printf BAD; printf ESCAPED",
    "printf BAD || printf ESCAPED",
    "printf BAD & wait",
])
def test_missing_cwd_blocks_entire_script(shell_endpoint, script):
    """§CORE-02/shell-context：无效 cwd 不能被 ; / || / & 绕过。"""
    session, cwd = shell_endpoint
    with pytest.raises(rpm.CommandError) as failure:
        session.run(script, shell=True, cwd=cwd + "/missing", timeout=5)
    assert failure.value.returncode != 0
    assert not failure.value.stdout.strip()
    assert "missing" in failure.value.stderr


def test_environment_precedes_expansion_and_covers_script(shell_endpoint):
    """§CORE-02/shell-context：env 在展开前生效并传给后续命令和子 shell。"""
    session, cwd = shell_endpoint
    result = session.run(
        'printf "%s\\n" "$RPM_CONTEXT"; sh -c \'printf "%s\\n" "$RPM_CONTEXT"\'; cat marker',
        shell=True, cwd=cwd, env={"RPM_CONTEXT": "中文 a'$b"}, timeout=5,
    )
    assert result.stdout.splitlines() == ["中文 a'$b", "中文 a'$b", "in-cwd"]


def test_path_override_does_not_prevent_shell_start(shell_endpoint):
    """§CORE-02/shell-context：覆盖 PATH 后内建命令仍能解析执行。"""
    session, cwd = shell_endpoint
    path = "/redpymake-test-no-executables"
    result = session.run('printf "%s\\n" "$PATH"; pwd', shell=True, cwd=cwd,
                         env={"PATH": path}, timeout=5)
    assert result.stdout.splitlines() == [path, cwd]


@pytest.mark.parametrize("script", [
    "if test -f marker; then cat marker; fi # trailing comment",
    "cat marker\nprintf done",
    "(cat marker) & wait; exit 7",
])
def test_script_syntax_and_exit_status(shell_endpoint, script):
    """§CORE-02/shell-context/background：复合脚本按原样解析，显式 wait 后保留退出码。"""
    session, cwd = shell_endpoint
    result = session.run(script, shell=True, cwd=cwd, check=False, timeout=5)
    assert "in-cwd" in result.stdout
    assert result.rc == (7 if "exit 7" in script else 0)


def test_shell_false_keeps_arguments_literal(shell_endpoint):
    """§CORE-02/shell-context：包装器不对 shell=False 参数进行二次展开。"""
    session, cwd = shell_endpoint
    args = ["$RPM_CONTEXT", "*.txt", "~", "a; false", "中文 a'b"]
    result = session.run("printf", "%s\n", *args, cwd=cwd,
                         env={"RPM_CONTEXT": "expanded"}, timeout=5)
    assert result.stdout.splitlines() == args
