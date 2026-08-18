"""CORE-07 统一异常（doc/core-lib-requirements.md § CORE-07）。

规格映射：
    §CORE-07/hierarchy                     → test_exception_hierarchy
    §CORE-07/reexport                      → test_all_exceptions_reexported
    §CORE-07/command-error/fields          → test_command_error_fields
    §CORE-07/command-timeout/fields        → test_command_timeout_fields
    §CORE-07/log-wait-timeout/fields       → test_log_wait_timeout_fields
    §CORE-07/transfer-error/fields         → test_transfer_error_fields
    §CORE-07/session-connection/fields     → test_session_connection_error_fields
    §CORE-07/input-not-found/fields        → test_input_not_found_fields
    §CORE-07/no-paramiko-leak              → test_ssh_does_not_leak_paramiko_exception
    §CORE-07/no-runtime-error              → test_wait_timeout_is_not_runtime_error
"""

from __future__ import annotations

import pytest

import redpymake as rpm
from redpymake.exceptions import (
    CommandError,
    CommandTimeoutError,
    InputNotFoundError,
    LogWaitTimeoutError,
    RedPyMakeError,
    ResourceError,
    ResourceNotFoundError,
    SessionClosedError,
    SessionConnectionError,
    SessionError,
    TransferError,
    UnsupportedOperationError,
)

from ._helpers.ssh_mock import install_paramiko_stub


# ------------------------------------------------------------------- 层级


def test_exception_hierarchy():
    """§CORE-07：所有异常必须继承 ``RedPyMakeError``，层级如需求文档所示。"""
    assert issubclass(SessionError, RedPyMakeError)
    assert issubclass(SessionConnectionError, SessionError)
    assert issubclass(SessionClosedError, SessionError)
    assert issubclass(CommandError, RedPyMakeError)
    assert issubclass(CommandTimeoutError, CommandError)
    assert issubclass(TransferError, RedPyMakeError)
    assert issubclass(ResourceError, RedPyMakeError)
    assert issubclass(ResourceNotFoundError, ResourceError)
    assert issubclass(InputNotFoundError, ResourceError)
    assert issubclass(LogWaitTimeoutError, RedPyMakeError)
    assert issubclass(UnsupportedOperationError, RedPyMakeError)


def test_all_exceptions_reexported():
    """§CORE-07：所有异常都能从 ``rpm`` 顶层导出。"""
    assert rpm.CommandError is CommandError
    assert rpm.CommandTimeoutError is CommandTimeoutError
    assert rpm.SessionConnectionError is SessionConnectionError
    assert rpm.SessionClosedError is SessionClosedError
    assert rpm.TransferError is TransferError
    assert rpm.ResourceNotFoundError is ResourceNotFoundError
    assert rpm.InputNotFoundError is InputNotFoundError
    assert rpm.LogWaitTimeoutError is LogWaitTimeoutError
    assert rpm.UnsupportedOperationError is UnsupportedOperationError


# ---------------------------------------------------- 各类异常字段


def test_command_error_fields():
    """§CORE-07：``CommandError`` 携带 command / returncode / stdout / stderr。"""
    exc = CommandError(
        "boom",
        command=("echo", "hi"),
        returncode=42,
        stdout="hi\n",
        stderr="ouch",
    )
    assert exc.command == ("echo", "hi")
    assert exc.returncode == 42
    assert exc.stdout == "hi\n"
    assert exc.stderr == "ouch"


def test_command_timeout_fields(local_session, python_probe):
    """§CORE-07：``CommandTimeoutError`` 携带 ``timeout`` 与部分 stdout/stderr。"""
    with pytest.raises(CommandTimeoutError) as ei:
        local_session.run(
            *python_probe(
                "import sys, time; sys.stdout.write('PARTIAL\\n');"
                " sys.stdout.flush(); time.sleep(3)"
            ),
            timeout=0.3,
        )
    exc = ei.value
    assert exc.timeout == 0.3
    assert exc.command  # 非空
    # 允许 stdout 未完全捕获，但类型必须是 str
    assert isinstance(exc.stdout, str)
    assert isinstance(exc.stderr, str)


def test_log_wait_timeout_fields(local_session):
    """§CORE-07：``LogWaitTimeoutError`` 必须暴露 pattern/timeout/records/output/command_result。"""
    with pytest.raises(LogWaitTimeoutError) as ei:
        local_session.wait("nope", timeout=0.1)
    exc = ei.value
    assert exc.pattern == "nope"
    assert exc.timeout == 0.1
    assert isinstance(exc.records, tuple)
    assert isinstance(exc.output, str)
    assert exc.command_result is None
    assert exc.data is None


def test_transfer_error_fields(fake_remote, local_session, tmp_path):
    """§CORE-07：``TransferError`` 携带 ``source`` / ``target``。"""
    src = tmp_path / "s.txt"
    src.write_text("ok")
    with pytest.raises(TransferError) as ei:
        local_session.push(
            local_session.path(str(src)), fake_remote.path("/tmp/x")
        )
    assert ei.value.source is not None
    assert ei.value.target is not None


def test_session_connection_error_fields(monkeypatch):
    """§CORE-07：``SessionConnectionError`` 携带 ``cause`` 且保留 ``__cause__``。"""
    install_paramiko_stub(monkeypatch, connect_raises=ConnectionRefusedError("nope"))
    with pytest.raises(SessionConnectionError) as ei:
        rpm.ssh("127.0.0.1", user="x")
    assert isinstance(ei.value.cause, ConnectionRefusedError)
    assert isinstance(ei.value.__cause__, ConnectionRefusedError)


def test_input_not_found_fields(local_session, tmp_path):
    """§CORE-07：``InputNotFoundError`` 携带 ``path`` 与 ``name``。"""
    with pytest.raises(InputNotFoundError) as ei:
        rpm.stale(
            local_session.path(str(tmp_path / "t")),
            depends_on=local_session.path(str(tmp_path / "missing")),
            name="deploy",
        )
    assert ei.value.name == "deploy"
    assert ei.value.path is not None


# ---------------------------------------------- 不泄露第三方 / 通用异常


def test_ssh_does_not_leak_paramiko_exception(monkeypatch):
    """§CORE-07：SSH 连接失败必须转成 ``SessionConnectionError``，不裸抛第三方异常。"""

    class ParamikoBoom(Exception):
        pass

    install_paramiko_stub(monkeypatch, connect_raises=ParamikoBoom("orig"))
    with pytest.raises(SessionConnectionError):
        rpm.ssh("127.0.0.1", user="x")


def test_wait_timeout_is_not_runtime_error(local_session):
    """§CORE-07：wait 超时不得使用 ``RuntimeError`` 之类通用异常。"""
    with pytest.raises(LogWaitTimeoutError) as ei:
        local_session.wait("nope", timeout=0.05)
    assert not isinstance(ei.value, RuntimeError)
    assert isinstance(ei.value, RedPyMakeError)
