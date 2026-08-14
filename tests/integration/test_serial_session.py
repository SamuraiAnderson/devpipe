"""串口会话集成测试（``pytest -m integration``）。

需要环境：本机有可访问的串口设备（例如接了 USB TTL 转换器的回环插头，或
真实开发板）。使用 ``RPM_TEST_SERIAL_PORT`` / ``RPM_TEST_SERIAL_BAUD``。
"""

from __future__ import annotations

import os

import pytest

import redpymake as rpm


pytestmark = pytest.mark.integration


def test_serial_open_and_write(serial_port):
    """§CORE-01：串口会话构造成功，可写入不抛异常。"""
    baud = int(os.environ.get("RPM_TEST_SERIAL_BAUD", "115200"))
    with rpm.serial(serial_port, baudrate=baud) as sess:
        # 写入一行；即使没有回响也应返回 CommandResult
        r = sess.run("echo", "hello", shell=True)
        assert isinstance(r, rpm.CommandResult)
