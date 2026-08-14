"""包顶层 API 契约 (CORE-08)。"""

from __future__ import annotations

import pkgutil

import redpymake as rpm


def test_public_api_names():
    expected = {
        "local", "ssh", "adb", "serial",
        "Session", "LocalSession", "SshSession", "AdbSession", "SerialSession",
        "CommandResult", "TransferResult", "ResourcePath", "ResourceStat",
        "SessionLogs", "SessionLogRecord", "LogBuffer", "LogCursor", "LogMatch",
        "stale", "StalePredicate", "PathSpec",
        "RedPyMakeError", "SessionError", "SessionConnectionError",
        "SessionClosedError", "CommandError", "CommandTimeoutError",
        "TransferError", "ResourceError", "ResourceNotFoundError",
        "InputNotFoundError", "LogWaitTimeoutError", "UnsupportedOperationError",
        "__version__",
    }
    missing = expected - set(rpm.__all__)
    assert not missing, f"missing from __all__: {missing}"


def test_lazy_optional_platforms_accessible_without_deps():
    # 即使未安装 paramiko，通过属性访问 SshSession 类型应可获得
    # （构造它才会检查依赖）
    assert isinstance(rpm.SshSession, type)
    assert isinstance(rpm.SerialSession, type)
    assert isinstance(rpm.AdbSession, type)


def test_version_string():
    assert isinstance(rpm.__version__, str)
    assert rpm.__version__.count(".") >= 2


def test_py_typed_included():
    import redpymake

    pkg_dir = pkgutil.get_loader("redpymake").get_filename()
    # get_filename returns the __init__.py path; look for py.typed sibling
    import os

    assert os.path.exists(
        os.path.join(os.path.dirname(pkg_dir), "py.typed")
    ), "py.typed marker must be shipped for PEP 561 support"
