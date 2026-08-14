"""SSH 会话集成测试（``pytest -m integration``）。

需要环境：可访问的 SSH 主机 + ``paramiko`` 已安装。
使用环境变量提供凭据，见 ``tests/integration/conftest.py``。
"""

from __future__ import annotations

import pytest

import redpymake as rpm


pytestmark = pytest.mark.integration


def test_ssh_run_echo(ssh_target):
    """§CORE-01/02：SSH 连接成功后可执行简单命令。"""
    with rpm.ssh(**ssh_target) as sess:
        r = sess.run("echo", "hi")
        assert "hi" in r.stdout
        assert r.ok


def test_ssh_push_pull_round_trip(ssh_target, tmp_path):
    """§CORE-04：本地 → SSH → 本地 的 push/pull 往返。"""
    src = tmp_path / "up.bin"
    dst = tmp_path / "down.bin"
    src.write_bytes(b"RPM-INT")
    with rpm.ssh(**ssh_target) as sess:
        remote = sess.path("/tmp/redpymake-int-probe.bin")
        with rpm.local() as local:
            sess.push(local.path(str(src)), remote)
            sess.pull(remote, local.path(str(dst)))
            sess.run("rm", "-f", "/tmp/redpymake-int-probe.bin")
    assert dst.read_bytes() == b"RPM-INT"
