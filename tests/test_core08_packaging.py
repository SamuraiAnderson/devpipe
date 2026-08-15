"""CORE-08 Python 库化（doc/core-lib-requirements.md § CORE-08）。

规格映射：
    §CORE-08/__all__/complete           → test_public_api_names
    §CORE-08/py.typed                   → test_py_typed_shipped
    §CORE-08/version                    → test_version_string
    §CORE-08/lazy-platform-attrs        → test_lazy_platform_classes_accessible
    §CORE-08/no-heavy-deps              → test_no_heavy_deps_in_metadata
    §CORE-08/no-basic-config            → test_library_does_not_call_basic_config
    §CORE-08/import-path                → test_top_level_import_works
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil

import redpymake as rpm


EXPECTED_PUBLIC = {
    "local", "ssh", "adb", "serial", "wsl",
    "script", "workspace", "discover",
    "Session", "LocalSession", "SshSession", "AdbSession", "SerialSession",
    "WslSession", "ScriptRun", "ScriptSnapshot", "ScriptCard",
    "Workspace", "WorkspaceLog", "WorkspaceRun",
    "CommandResult", "TransferResult", "ResourcePath", "ResourceStat",
    "SessionLogs", "SessionLogRecord", "LogBuffer", "LogCursor", "LogMatch",
    "stale", "StalePredicate", "PathSpec",
    "RedPyMakeError", "SessionError", "SessionConnectionError",
    "SessionClosedError", "CommandError", "CommandTimeoutError",
    "TransferError", "ResourceError", "ResourceNotFoundError",
    "InputNotFoundError", "LogWaitTimeoutError", "UnsupportedOperationError",
    "__version__",
}


def test_public_api_names():
    """§CORE-08：``__all__`` 完整定义顶层 API。"""
    missing = EXPECTED_PUBLIC - set(rpm.__all__)
    assert not missing, f"missing from __all__: {missing}"


def test_py_typed_shipped():
    """§CORE-08：PEP 561 兼容——包内必须有 ``py.typed`` 标记文件。"""
    pkg_init = pkgutil.get_loader("redpymake").get_filename()
    marker = os.path.join(os.path.dirname(pkg_init), "py.typed")
    assert os.path.exists(marker), "py.typed marker must be shipped"


def test_version_string():
    """§CORE-08：``__version__`` 为形如 ``X.Y.Z`` 的字符串。"""
    assert isinstance(rpm.__version__, str)
    assert rpm.__version__.count(".") >= 2


def test_lazy_platform_classes_accessible():
    """§CORE-08：可选平台会话类通过属性访问可获取（构造时才检查依赖）。"""
    assert isinstance(rpm.SshSession, type)
    assert isinstance(rpm.AdbSession, type)
    assert isinstance(rpm.SerialSession, type)


def test_no_heavy_deps_in_metadata():
    """§CORE-08：核心依赖不得包含 Streamlit / NumPy / SciPy / pydub。"""
    dist = importlib.metadata.distribution("redpymake")
    requires = dist.requires or []
    lower = " ".join(r.lower() for r in requires)
    for banned in ("streamlit", "numpy", "scipy", "pydub"):
        assert banned not in lower, (
            f"{banned!r} must not appear in core dependencies; got: {requires}"
        )


def test_library_does_not_call_basic_config():
    """§CORE-08：库本身不得调用 ``logging.basicConfig()``。

    以"重新加载 redpymake 前后，root logger 的 handlers 不变"作为可测代理。
    """
    root_handlers_before = list(logging.getLogger().handlers)
    importlib.reload(rpm)
    root_handlers_after = list(logging.getLogger().handlers)
    assert root_handlers_after == root_handlers_before


def test_top_level_import_works():
    """§CORE-08：``import redpymake as rpm`` 即可访问所有顶层 API。"""
    m = importlib.import_module("redpymake")
    assert hasattr(m, "local")
    assert hasattr(m, "stale")
