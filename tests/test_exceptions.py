"""统一异常层次的基本形状 (CORE-07)。"""

from __future__ import annotations

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


def test_hierarchy():
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


def test_command_error_fields():
    exc = CommandError(
        "boom",
        command=("echo", "hi"),
        returncode=42,
        stdout="hi\n",
        stderr="",
    )
    assert exc.command == ("echo", "hi")
    assert exc.returncode == 42
    assert exc.stdout == "hi\n"


def test_reexported_from_package():
    assert rpm.CommandError is CommandError
    assert rpm.SessionConnectionError is SessionConnectionError
