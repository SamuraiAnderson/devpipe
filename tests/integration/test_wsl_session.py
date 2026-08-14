"""WSL 会话集成测试（``pytest -m integration``）。

需要环境：本机装有 Windows Subsystem for Linux，且至少有一个默认发行版。
使用环境变量控制启用与凭据，见 ``tests/integration/conftest.py``。
"""

from __future__ import annotations

import pytest

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
