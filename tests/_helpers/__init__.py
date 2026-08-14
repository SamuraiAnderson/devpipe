"""测试辅助工具包。

- ``fake_remote``：``FakeRemoteSession``，用于测试跨会话传输语义而无需真实网络。
- ``ssh_mock``：注入 paramiko stub 以便在没有 paramiko 的环境下测 ``SshSession`` 契约。
- ``log_probe``：日志断言便捷函数。
"""

from __future__ import annotations
