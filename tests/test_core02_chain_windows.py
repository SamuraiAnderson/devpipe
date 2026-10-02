"""Specification mapping: §CORE-02/chain → Windows native argv and binary pipes."""
import json
import os
import sys
import pytest
import redpymake as rpm
from ._helpers.chain_host import install_host, _SERIAL_STUB

pytestmark = pytest.mark.skipif(os.name != "nt", reason="executes Windows PowerShell")


def test_windows_ssh_native_argv_and_environment(monkeypatch, tmp_path):
    """§CORE-02/chain：真实 PowerShell 包装器保留引号、空格、环境和终点 cwd。"""
    wire = install_host(monkeypatch, platform="windows")
    wire.native = True
    args = ["a b", 'a"b', "$HOME", "*.txt", "a'b", "tail\\", "", "中文"]
    code = "import os,sys,json;print(json.dumps([sys.argv[1:],os.getcwd(),os.getenv('RPM_TEST_VAR')]))"
    with rpm.ssh("windows") as host:
        result = host.run(sys.executable, "-c", code, *args, cwd=str(tmp_path),
                          env={"RPM_TEST_VAR": "a'$b"}, timeout=10, check=False)
        assert result.rc == 0, result.stderr
        assert json.loads(result.stdout) == [args, str(tmp_path), "a'$b"]


def test_windows_uart_binary_bridge(monkeypatch, tmp_path):
    """§CORE-06/chain：真实 Windows 管道传递 UART 所有字节，不经文本重编码。"""
    wire = install_host(monkeypatch, platform="windows")
    wire.native = True
    (tmp_path / "serial.py").write_text(_SERIAL_STUB + "\nSerial = Port\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    payload = bytes(range(256)) * 3
    with rpm.ssh("windows").serial("echo", python_path=sys.executable) as uart:
        assert uart.run(payload, timeout=5).wait(payload, timeout=3).text == payload


def test_windows_shell_unicode_and_exit(monkeypatch):
    """§CORE-02/chain：Windows shell 文本使用 UTF-8，保留退出码并无包装器噪声。"""
    wire = install_host(monkeypatch, platform="windows")
    wire.native = True
    with rpm.ssh("windows") as host:
        result = host.run("Write-Output '中文'; exit 7", shell=True, check=False, timeout=10)
        assert result.stdout == "中文"
        assert result.stderr == ""
        assert result.rc == 7
