"""Specification mapping:
§CORE-02/encoding → WSL launcher UTF-8 and explicit GBK/UTF-8 command bytes.
§CORE-07/diagnostics → error fields and SessionLogs preserve Unicode.
"""
import os
import subprocess
import sys

import pytest
import redpymake as rpm

from ._helpers.chain_host import install_host, Reply


_DIAGNOSTIC = "找不到指定的分发。\nError code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND"


@pytest.mark.parametrize("operation", ["run", "mkdir", "home"])
def test_wsl_launcher_diagnostics_are_utf8(monkeypatch, operation):
    """§CORE-02/encoding：WSL 启动器诊断经 run/path/文件操作可读且环境不外泄。"""
    monkeypatch.setenv("WSL_UTF8", "0")
    native_popen = subprocess.Popen
    code = (
        "import os,sys; "
        f"message={_DIAGNOSTIC!r}; "
        "codec='utf-8' if os.environ.get('WSL_UTF8') == '1' else 'utf-16le'; "
        "sys.stderr.buffer.write(message.encode(codec)); sys.exit(1)"
    )

    def launch(argv, *args, **kwargs):
        if argv[0] == "rpm-encoding-wsl":
            argv = [sys.executable, "-c", code]
        return native_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with rpm.wsl(wsl_path="rpm-encoding-wsl") as session:
        with pytest.raises((rpm.CommandError, rpm.TransferError)) as failure:
            if operation == "run":
                session.run("true", timeout=5)
            elif operation == "mkdir":
                session.path("/example").mkdir()
            else:
                session.path("~/example")
        assert failure.value.stderr.strip() == _DIAGNOSTIC
        if operation == "run":
            assert "找不到指定的分发。" in session.logs.text()
    assert os.environ["WSL_UTF8"] == "0"


def test_chained_wsl_launcher_diagnostics_are_utf8(monkeypatch):
    """§CORE-02/encoding：SSH → WSL 在 Windows 宿主设置启动器编码。"""
    wire = install_host(monkeypatch, platform="windows")
    with rpm.ssh("host").wsl() as session:
        def respond(command):
            codec = "utf-8" if "WSL_UTF8" in command else "utf-16le"
            return Reply(err=_DIAGNOSTIC.encode(codec), rc=1)
        wire.handler = respond
        with pytest.raises(rpm.CommandError) as failure:
            session.run("true", timeout=5)
        assert failure.value.stderr == _DIAGNOSTIC
        assert "找不到指定的分发。" in session.logs.text()


@pytest.mark.parametrize("encoding", ["gbk", "utf-8"])
@pytest.mark.parametrize("kind", ["local", "wsl"])
def test_command_error_and_logs_preserve_chinese(monkeypatch, tmp_path, encoding, kind):
    """§CORE-02/encoding/CORE-07/diagnostics：显式编码同时适用于异常字段和日志。"""
    message = "无法打开文件：中文目录/构建失败.txt"
    code = f"import sys; sys.stderr.buffer.write({message.encode(encoding)!r}); sys.exit(7)"
    if kind == "wsl":
        native_popen = subprocess.Popen
        def launch(argv, *args, **kwargs):
            if argv[0] == "rpm-encoding-wsl":
                argv = [sys.executable, "-c", code]
            return native_popen(argv, *args, **kwargs)
        monkeypatch.setattr(subprocess, "Popen", launch)
    with (rpm.local() if kind == "local" else rpm.wsl(wsl_path="rpm-encoding-wsl")) as session:
        with pytest.raises(rpm.CommandError) as failure:
            session.run(sys.executable, "-c", code, encoding=encoding, timeout=5)
        assert failure.value.returncode == 7
        assert failure.value.stderr == message
        assert message in session.logs.text()
