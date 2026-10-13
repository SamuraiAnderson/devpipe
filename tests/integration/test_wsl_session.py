"""WSL 会话集成测试（``pytest -m integration``）。

需要环境：本机装有 Windows Subsystem for Linux，且至少有一个默认发行版。
使用环境变量控制启用与凭据，见 ``tests/integration/conftest.py``。
"""

from __future__ import annotations

import pytest
import uuid

import redpymake as rpm


pytestmark = pytest.mark.integration


def test_wsl_echo(wsl_target):
    """§CORE-01/02：WSL 会话构造成功后可执行简单命令。"""
    with rpm.wsl(**wsl_target) as sess:
        r = sess.run("echo", "hi")
        assert "hi" in r.stdout
        assert r.ok


def test_wsl_run_with_cwd(wsl_target):
    """§CORE-02：``at()`` 视图给命令注入 ``cd`` 前缀，``pwd`` 应回显目标目录。"""
    with rpm.wsl(**wsl_target) as sess:
        r = sess.at("/tmp").run("pwd")
        assert "/tmp" in r.stdout


def test_wsl_push_pull_round_trip(wsl_target, tmp_path):
    """§CORE-04：本地 → WSL → 本地 的 push/pull 往返，字节应一致。"""
    src = tmp_path / "up.bin"
    dst = tmp_path / "down.bin"
    src.write_bytes(b"WSL-INT")
    with rpm.wsl(**wsl_target) as sess:
        remote = sess.path("/tmp/redpymake-int-probe.bin")
        with rpm.local() as local:
            sess.push(local.path(str(src)), remote)
            sess.pull(remote, local.path(str(dst)))
            sess.run("rm", "-f", "/tmp/redpymake-int-probe.bin")
    assert dst.read_bytes() == b"WSL-INT"


def test_wsl_shell_context_and_explicit_background_wait(wsl_target):
    """§CORE-02/shell-context/background：真实 WSL 的 env/复合脚本/显式 wait。"""
    with rpm.wsl(**wsl_target) as sess:
        result = sess.run(
            'printf "%s\\n" "$RPM_TEST_VALUE"; '
            'sh -c \'printf "%s\\n" "$RPM_TEST_VALUE"\' & wait "$!"; pwd',
            shell=True, cwd="/tmp", env={"RPM_TEST_VALUE": "中文 value"}, timeout=15,
        )
        assert result.stdout.splitlines() == ["中文 value", "中文 value", "/tmp"]
        literal = "中文 spaces $HOME 'quote' \"double\" ; * &"
        assert sess.run("printf", "%s", literal, timeout=10).stdout == literal
        assert sess.run('printf "%s" "$PATH"', shell=True, env={"PATH": "/nonexistent"},
                        timeout=10).stdout == "/nonexistent"
        missing = "/tmp/redpymake-missing-" + uuid.uuid4().hex
        for script in ["printf BAD; printf ESCAPED", "printf BAD || printf ESCAPED", "printf BAD & wait"]:
            with pytest.raises(rpm.CommandError) as failure:
                sess.run(script, shell=True, cwd=missing, timeout=10)
            assert not failure.value.stdout.strip()


def test_wsl_launcher_error_is_readable(wsl_target):
    """§CORE-02/encoding：真实启动器的发行版缺失诊断没有 UTF-16 NUL 或替换字符。"""
    with rpm.wsl(distribution="redpymake-missing-" + uuid.uuid4().hex) as sess:
        with pytest.raises(rpm.CommandError) as failure:
            sess.run("true", timeout=10)
        output = failure.value.stdout + failure.value.stderr
        assert "WSL_E_DISTRO_NOT_FOUND" in output
        assert "\0" not in output and "\ufffd" not in output
