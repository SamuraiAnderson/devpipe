"""注入 paramiko stub，用于在无 paramiko 的 CI 环境下测试 SSH 契约层。

只覆盖 ``SshSession.__init__`` 路径上会用到的最小面：``SSHClient``、
``AutoAddPolicy``。默认 ``connect`` 直接成功；传 ``connect_raises`` 时
在 ``connect`` 中抛出该异常，用于覆盖 ``SessionConnectionError`` 分支。
"""

from __future__ import annotations

import sys
import types
from typing import Any


def install_paramiko_stub(monkeypatch, *, connect_raises: BaseException | None = None) -> Any:
    """安装 fake paramiko 模块。

    调用后 ``import paramiko`` 会拿到本 stub。测试结束（``monkeypatch``
    fixture teardown）会自动移除，恢复原始 ``sys.modules`` 状态。
    """

    fake = types.ModuleType("paramiko")

    class _AutoAddPolicy:
        """Stub paramiko.AutoAddPolicy。"""

    class _Transport:
        def is_active(self) -> bool:
            return True

        def open_session(self):
            raise IOError("stub transport cannot open sessions")

    class _SSHClient:
        def __init__(self) -> None:
            self._transport = _Transport()

        def set_missing_host_key_policy(self, policy) -> None:  # noqa: D401
            return None

        def connect(self, **kwargs) -> None:
            if connect_raises is not None:
                raise connect_raises

        def get_transport(self):
            return self._transport

        def open_sftp(self):
            raise IOError("stub cannot open sftp")

        def close(self) -> None:
            return None

    fake.SSHClient = _SSHClient
    fake.AutoAddPolicy = _AutoAddPolicy
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    return fake


__all__ = ["install_paramiko_stub"]
