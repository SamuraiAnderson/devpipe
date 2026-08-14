"""集成测试专用 fixture。

集成测试通过环境变量提供凭据；缺失时自动 skip 而不是失败。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def ssh_target() -> dict:
    """SSH 集成目标。

    需要环境变量 ``RPM_TEST_SSH_HOST``；可选 ``RPM_TEST_SSH_USER``、
    ``RPM_TEST_SSH_PORT``、``RPM_TEST_SSH_KEY``、``RPM_TEST_SSH_PASSWORD``。
    """
    host = os.environ.get("RPM_TEST_SSH_HOST")
    if not host:
        pytest.skip(
            "set RPM_TEST_SSH_HOST (+ optional _USER/_PORT/_KEY/_PASSWORD) "
            "to enable SSH integration tests"
        )
    return {
        "host": host,
        "user": os.environ.get("RPM_TEST_SSH_USER"),
        "port": int(os.environ.get("RPM_TEST_SSH_PORT", "22")),
        "password": os.environ.get("RPM_TEST_SSH_PASSWORD"),
        "key_filename": os.environ.get("RPM_TEST_SSH_KEY"),
    }


@pytest.fixture
def adb_serial() -> str | None:
    """ADB 集成序列号；未提供时使用默认设备（若接了）。

    设置 ``RPM_TEST_ADB=1`` 才启用；未设置直接 skip。
    """
    if not os.environ.get("RPM_TEST_ADB"):
        pytest.skip("set RPM_TEST_ADB=1 to enable ADB integration tests")
    return os.environ.get("RPM_TEST_ADB_SERIAL")


@pytest.fixture
def serial_port() -> str:
    """串口集成端口，来自 ``RPM_TEST_SERIAL_PORT``；未设置直接 skip。"""
    port = os.environ.get("RPM_TEST_SERIAL_PORT")
    if not port:
        pytest.skip(
            "set RPM_TEST_SERIAL_PORT (e.g. COM3 or /dev/ttyUSB0) "
            "to enable serial integration tests"
        )
    return port


@pytest.fixture
def wsl_target() -> dict:
    """WSL 集成目标。

    需要环境变量 ``RPM_TEST_WSL=1``；可选 ``RPM_TEST_WSL_DISTRO``、
    ``RPM_TEST_WSL_USER``。未设置直接 skip。
    """
    if not os.environ.get("RPM_TEST_WSL"):
        pytest.skip(
            "set RPM_TEST_WSL=1 (+ optional _DISTRO/_USER) "
            "to enable WSL integration tests"
        )
    return {
        "distribution": os.environ.get("RPM_TEST_WSL_DISTRO"),
        "user": os.environ.get("RPM_TEST_WSL_USER"),
    }
