"""CORE-01 统一会话（doc/core-lib-requirements.md § CORE-01）。

规格映射：
    §CORE-01/factory/local            → test_local_factory_returns_local_session
    §CORE-01/factory/type-name        → test_factory_type_names
    §CORE-01/lifecycle/context        → test_local_context_manager
    §CORE-01/lifecycle/close-idem     → test_close_is_idempotent
    §CORE-01/lifecycle/closed-op      → test_closed_session_rejects_operations
    §CORE-01/lifecycle/closed-logs    → test_closed_session_still_reads_logs
    §CORE-01/at/shares-connection     → test_at_view_shares_connection_and_buffer
    §CORE-01/at/does-not-mutate       → test_at_does_not_mutate_original
    §CORE-01/at/chained               → test_at_can_be_chained
    §CORE-01/no-global-registry       → test_no_implicit_global_registry
    §CORE-01/ssh/connect-immediately  → test_ssh_connect_failure_wraps_in_session_connection_error
    §CORE-01/adb/no-adb-binary        → test_adb_without_binary_raises_session_connection_error
    §CORE-01/serial/no-fs             → test_serial_resource_ops_raise_unsupported
    §CORE-01/serial/run-str-no-crlf   → test_serial_run_str_does_not_append_newline
    §CORE-01/serial/run-bytes         → test_serial_run_bytes_writes_raw
    §CORE-01/serial/no-timeout        → test_serial_signature_has_no_timeout
    §CORE-01/serial/streaming         → test_serial_streaming_rx_immediate
    §CORE-01/serial/idle-flush        → test_serial_prompt_idle_flush
    §CORE-01/serial/stream-cursor     → test_serial_stream_cursor_catches_pre_wait_history_and_advances
    §CORE-01/wsl/no-wsl-binary        → test_wsl_without_binary_raises_session_connection_error

"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import (
    SessionClosedError,
    SessionConnectionError,
    UnsupportedOperationError,
)

from ._helpers.serial_stub import open_stub_serial
from ._helpers.ssh_mock import install_paramiko_stub


# --------------------------------------------------------------------- 工厂


def test_local_factory_returns_local_session():
    """§CORE-01：``rpm.local()`` 返回 ``LocalSession`` 且构造后即可用。"""
    with rpm.local() as sess:
        assert isinstance(sess, rpm.LocalSession)
        assert sess.kind == "local"
        assert sess.closed is False


def test_factory_type_names():
    """§CORE-01：五类会话的类型名可通过顶层属性获取（惰性也算）。

    附带 §CORE-09：``rpm.script`` 工厂与 ``rpm.ScriptRun`` 类型也走同一契约。
    """
    assert isinstance(rpm.LocalSession, type)
    assert isinstance(rpm.SshSession, type)
    assert isinstance(rpm.AdbSession, type)
    assert isinstance(rpm.SerialSession, type)
    assert isinstance(rpm.WslSession, type)
    assert callable(rpm.script)
    assert isinstance(rpm.ScriptRun, type)
    assert isinstance(rpm.ScriptSnapshot, type)


# ------------------------------------------------------------------- 生命周期


def test_local_context_manager():
    """§CORE-01：会话必须支持 ``with`` 语法，退出时自动 close。"""
    sess = rpm.local()
    with sess as ctx:
        assert ctx is sess
        assert not sess.closed
    assert sess.closed


def test_close_is_idempotent():
    """§CORE-01：``close()`` 必须可重复调用而不抛异常。"""
    sess = rpm.local()
    sess.close()
    sess.close()
    sess.close()


def test_closed_session_rejects_operations(python_probe):
    """§CORE-01：关闭后继续操作抛 ``SessionClosedError``。"""
    sess = rpm.local()
    sess.close()
    with pytest.raises(SessionClosedError):
        sess.run(*python_probe("pass"))
    with pytest.raises(SessionClosedError):
        sess.at(".")


def test_closed_session_still_reads_logs():
    """§CORE-01：会话关闭后仍可读取已收集的日志（对应验收标准 12）。"""
    sess = rpm.local()
    # 关闭前至少有 session_open 事件
    assert sess.logs.records()
    sess.close()
    records = sess.logs.records()
    assert any(r.event == "session_open" for r in records)
    assert any(r.event == "session_closed" for r in records)


# --------------------------------------------------------------------- at()


def test_at_view_shares_connection_and_buffer(tmp_workspace: rpm.LocalSession):
    """§CORE-01：``at()`` 创建的视图共享底层连接与日志缓冲。"""
    view = tmp_workspace.at(".")
    assert view.logs.buffer is tmp_workspace.logs.buffer
    assert view.root is tmp_workspace
    assert view.is_view is True


def test_at_does_not_mutate_original(tmp_path: Path):
    """§CORE-01：``at()`` 不修改原会话的默认工作目录。"""
    (tmp_path / "sub").mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        view = sess.at("sub")
        assert Path(view.default_cwd).resolve() == (tmp_path / "sub").resolve()
        assert Path(sess.default_cwd).resolve() == tmp_path.resolve()


def test_at_can_be_chained(tmp_path: Path):
    """§CORE-01：``at()`` 可链式调用得到嵌套视图。"""
    (tmp_path / "a" / "b").mkdir(parents=True)
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        deep = sess.at("a").at("b")
        assert Path(deep.default_cwd).resolve() == (tmp_path / "a" / "b").resolve()
        assert deep.root is sess


# ------------------------------------------------------------ 全局注册表约束


def test_no_implicit_global_registry():
    """§CORE-01：构造会话不得隐式注册到全局 UI 注册表。

    该项以"负面"方式验证：构造多个会话后不应影响 ``__all__`` 中未列出的
    模块级状态；直接检查 ``rpm`` 模块无 ``registry`` / ``controllers`` 之类
    可疑属性。
    """
    before = set(dir(rpm))
    a = rpm.local()
    b = rpm.local()
    try:
        after = set(dir(rpm))
        # 允许延迟属性（例如 SshSession __getattr__ 会新增），但不应出现
        # "registry"、"sessions"、"controllers" 这类全局收集器名。
        forbidden = {"registry", "sessions", "controllers", "instances"}
        assert forbidden.isdisjoint(after - before)
    finally:
        a.close()
        b.close()


# --------------------------------------------------------- SSH / ADB / Serial / WSL


def test_ssh_connect_failure_wraps_in_session_connection_error(monkeypatch):
    """§CORE-01：SSH 构造时立即连接；失败必须抛 ``SessionConnectionError``。

    使用 paramiko stub 强制 ``connect`` 抛 ``TimeoutError``，验证不会泄漏
    原生异常。
    """
    install_paramiko_stub(monkeypatch, connect_raises=TimeoutError("boom"))
    with pytest.raises(SessionConnectionError) as ei:
        rpm.ssh("10.255.255.1", user="nobody")
    assert isinstance(ei.value.__cause__, TimeoutError)
    assert "10.255.255.1" in str(ei.value) or "boom" in str(ei.value)


def test_adb_without_binary_raises_session_connection_error(monkeypatch):
    """§CORE-01：``rpm.adb()`` 找不到 adb 命令时立即抛 ``SessionConnectionError``。"""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SessionConnectionError):
        rpm.adb()


def test_serial_resource_ops_raise_unsupported(monkeypatch):
    """§CORE-01：串口无文件系统概念，``_resource_*`` 抛 ``UnsupportedOperationError``。

    注入 pyserial stub 让构造成功，然后调用 ``session.path(...).exists()``
    应抛 ``UnsupportedOperationError``。
    """
    import sys
    import types

    fake = types.ModuleType("serial")

    class _StubPort:
        def __init__(self, *args, **kwargs):
            self._closed = False

        def read(self, n):
            return b""

        def write(self, data):
            return len(data)

        def flush(self):
            pass

        def close(self):
            self._closed = True

    fake.Serial = _StubPort
    monkeypatch.setitem(sys.modules, "serial", fake)

    sess = rpm.serial("COM_TEST")
    try:
        p = sess.path("/tmp/x")
        with pytest.raises(UnsupportedOperationError):
            p.exists()
    finally:
        sess.close()


def test_serial_run_str_does_not_append_newline(monkeypatch):
    """§CORE-01：串口 ``run(str)`` 默认不追加 ``\\r\\n``；显式 ``newline`` 才追加。"""
    with open_stub_serial(monkeypatch) as (sess, port):
        sess.run("reboot")
        assert bytes(port.written) == b"reboot"
    with open_stub_serial(monkeypatch) as (sess, port):
        sess.run("reboot\r")
        assert bytes(port.written) == b"reboot\r"
    with open_stub_serial(monkeypatch, newline="\r") as (sess, port):
        sess.run("reboot")
        assert bytes(port.written) == b"reboot\r"


def test_serial_run_bytes_writes_raw(monkeypatch):
    """§CORE-01：串口 ``run(bytes)`` 原样写入，不拼 newline。"""
    with open_stub_serial(monkeypatch, newline="\r") as (sess, port):
        sess.run(b"\xaa\x55")
        assert bytes(port.written) == b"\xaa\x55"
    with pytest.raises(TypeError):
        with open_stub_serial(monkeypatch) as (sess, _port):
            sess.run(b"\x01", "extra")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        with open_stub_serial(monkeypatch) as (sess, _port):
            sess.run(b"\x01", shell=True)


def test_serial_signature_has_no_timeout():
    """§CORE-01/serial/no-timeout：SerialSession 与 rpm.serial 接口不设 timeout 初始化参数，保证流式传输。"""
    import inspect

    sig_factory = inspect.signature(rpm.serial)
    assert "timeout" not in sig_factory.parameters
    sig_cls = inspect.signature(rpm.SerialSession)
    assert "timeout" not in sig_cls.parameters


def test_serial_streaming_rx_immediate(monkeypatch):
    """§CORE-01/serial/streaming：底层流式接收短字节片段，无需等待分块或超时。"""
    with open_stub_serial(monkeypatch) as (sess, port):
        # 即使只喂入 1 个字节，也能立刻匹配，不要求凑满 block 或等待固定底层超时
        port.feed(b"A")
        m1 = sess.wait(b"A", timeout=1.0)
        assert m1.text == b"A"

        # 连续追加流式数据
        port.feed(b"B")
        port.feed(b"C\n")
        m2 = sess.wait("BC", timeout=1.0)
        assert "BC" in m2.text


def test_serial_prompt_idle_flush(monkeypatch):
    """§CORE-01/serial/idle-flush：无 \\n 的提示符（如 login: ）在短空闲后自动冲刷入文本日志，支持 wait 匹配。"""
    with open_stub_serial(monkeypatch) as (sess, port):
        port.feed(b"login: ")
        # 无换行符，空闲超时后应能被普通文本 wait 匹配
        m = sess.wait("login: ", timeout=1.0)
        assert m.text == "login: "


def test_serial_stream_cursor_catches_pre_wait_history_and_advances(monkeypatch):
    """§CORE-01/serial/stream-cursor：流式消费游标自 0 开始，捕获调用 wait 前到达的历史并在匹配后推进。"""
    with open_stub_serial(monkeypatch) as (sess, port):
        # 1. 文本历史感知与推进
        port.feed(b"U-Boot 2024.01\r\n")
        port.feed(b"autoboot in 3s\r\n")
        # 即使在 wait 之前就已经到达，也能命中，避免时序竞态
        m1 = sess.wait("U-Boot", timeout=1.0)
        assert "U-Boot" in m1.text
        # 下一次独立 wait 自动从上一次命中之后继续，不会重复命中 U-Boot
        m2 = sess.wait("autoboot", timeout=1.0)
        assert "autoboot" in m2.text
        assert m2.record.sequence > m1.record.sequence

        # 2. 字节历史感知与推进
        port.feed(b"\xaa\x55\x01\x02")
        bm1 = sess.wait(b"\xaa\x55", timeout=1.0)
        assert bm1.text == b"\xaa\x55"
        bm2 = sess.wait(b"\x01\x02", timeout=1.0)
        assert bm2.text == b"\x01\x02"


def test_wsl_without_binary_raises_session_connection_error(monkeypatch):
    """§CORE-01：``rpm.wsl()`` 找不到 ``wsl.exe`` 时立即抛 ``SessionConnectionError``。

    WSL 会话被视作"本地已运行的 Linux 用户态"，构造时只校验 ``wsl.exe`` 存在；
    不做 distro 级探测。这里通过 monkeypatch 让 ``shutil.which`` 返回 ``None``，
    应立即抛出 ``SessionConnectionError``，与 ADB 语义一致。
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SessionConnectionError):
        rpm.wsl()
