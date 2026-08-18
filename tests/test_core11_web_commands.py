"""CORE-11 CommandBar 手动命令（doc/core-lib-requirements.md § CORE-11）。

规格映射：
    §CORE-11/web/commands/serial-cr              → test_commandbar_serial_appends_cr
    §CORE-11/web/commands/serial-cr-no-double    → test_commandbar_serial_does_not_double_cr
    §CORE-11/web/commands/serial-newline-set     → test_commandbar_serial_respects_session_newline
    §CORE-11/web/commands/serial-history         → test_commandbar_serial_history_keeps_original
    §CORE-11/web/commands/local-no-cr            → test_commandbar_local_does_not_append_cr
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import redpymake as rpm

from ._helpers.serial_stub import install_pyserial_stub

pytest.importorskip("fastapi")


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _execute(ws, session_id: str, command: str, **kwargs):
    ex = ws.command_executor
    cmd_id = ex.execute(
        session_id,
        command,
        shell=kwargs.get("shell", True),
        timeout=kwargs.get("timeout", 0.2),
    )
    assert _wait_until(lambda: cmd_id not in ex._active_commands)
    return ex


def test_commandbar_serial_appends_cr(monkeypatch, tmp_path: Path):
    """§CORE-11：串口 ``newline`` 为空时，CommandBar 提交的文本命令自动补 ``\\r``。"""
    port = install_pyserial_stub(monkeypatch)
    with rpm.workspace(tmp_path) as ws:
        sess = ws.serial("COM_TEST")
        _execute(ws, sess.session_id, "reboot")
        assert bytes(port.written) == b"reboot\r"


def test_commandbar_serial_does_not_double_cr(monkeypatch, tmp_path: Path):
    """§CORE-11：命令已以 ``\\r`` / ``\\n`` 结尾时不再补。"""
    port = install_pyserial_stub(monkeypatch)
    with rpm.workspace(tmp_path) as ws:
        sess = ws.serial("COM_TEST")
        _execute(ws, sess.session_id, "reboot\r")
        assert bytes(port.written) == b"reboot\r"


def test_commandbar_serial_respects_session_newline(monkeypatch, tmp_path: Path):
    """§CORE-11：会话已设非空 ``newline`` 时不补，避免与 ``run()`` 叠加。"""
    port = install_pyserial_stub(monkeypatch)
    with rpm.workspace(tmp_path) as ws:
        sess = ws.serial("COM_TEST", newline="\r")
        _execute(ws, sess.session_id, "reboot")
        assert bytes(port.written) == b"reboot\r"
    port2 = install_pyserial_stub(monkeypatch)
    with rpm.workspace(tmp_path) as ws:
        sess = ws.serial("COM_OTHER", newline="\r\n")
        _execute(ws, sess.session_id, "reboot")
        assert bytes(port2.written) == b"reboot\r\n"


def test_commandbar_serial_history_keeps_original(monkeypatch, tmp_path: Path):
    """§CORE-11：命令历史保存用户原文，不含自动补上的 ``\\r``。"""
    install_pyserial_stub(monkeypatch)
    with rpm.workspace(tmp_path) as ws:
        sess = ws.serial("COM_TEST")
        ex = _execute(ws, sess.session_id, "reboot")
        assert ex._history_pending is not None
        recs = ex._history_pending["sessions"][sess.session_id]
        assert recs[-1]["command"] == "reboot"


def test_commandbar_local_does_not_append_cr(tmp_path: Path):
    """§CORE-11：非串口会话不补 ``\\r``。"""
    with rpm.workspace(tmp_path) as ws:
        sess = ws.local()
        _execute(ws, sess.session_id, "python -c \"print('ok')\"")
        starts = [r for r in sess.logs.records() if r.event == "command_start"]
        assert starts
        argv = starts[-1].fields.get("argv")
        assert argv == ["python -c \"print('ok')\""]
