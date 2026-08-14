"""ADB 会话集成测试（``pytest -m integration``）。

需要环境：本机 ``adb`` 可用且至少有一台设备已连接。
"""

from __future__ import annotations

import pytest

import redpymake as rpm


pytestmark = pytest.mark.integration


def test_adb_shell_echo(adb_serial):
    """§CORE-01/02：ADB 连接成功后可执行简单 shell 命令。"""
    with rpm.adb(serial=adb_serial) as sess:
        r = sess.run("echo", "hi")
        assert "hi" in r.stdout


def test_adb_push_pull_round_trip(adb_serial, tmp_path):
    """§CORE-04：本地 → ADB → 本地 的 push/pull 往返。"""
    src = tmp_path / "up.bin"
    dst = tmp_path / "down.bin"
    src.write_bytes(b"ADB-INT")
    with rpm.adb(serial=adb_serial) as sess:
        remote = sess.path("/data/local/tmp/redpymake-int-probe.bin")
        with rpm.local() as local:
            sess.push(local.path(str(src)), remote)
            sess.pull(remote, local.path(str(dst)))
            sess.run("rm", "-f", "/data/local/tmp/redpymake-int-probe.bin")
    assert dst.read_bytes() == b"ADB-INT"
