"""Specification mapping: §CORE-06/chain → stream failures and wait wakeups."""
import threading
import sys
import time
import pytest
import redpymake as rpm
from ._helpers.serial_stub import install_pyserial_stub
from ._helpers.chain_host import install_host


@pytest.mark.parametrize("pattern", ["missing", b"missing"])
def test_serial_reader_failure_wakes_wait(monkeypatch, pattern):
    """§CORE-06/chain：串口读线程故障唤醒文本/字节等待，保留根因。"""
    port = install_pyserial_stub(monkeypatch)
    trigger = threading.Event()
    def read(count):
        trigger.wait(2)
        raise OSError("serial unplugged")
    port.read = read
    with rpm.serial("COM_TEST") as uart:
        trigger.set()
        with pytest.raises(rpm.SessionConnectionError, match="unplugged"):
            uart.wait(pattern, timeout=1)


def test_remote_uart_binary_transparency_and_fragmented_frames(monkeypatch):
    """§CORE-06/chain：真实桥接进程在分包和部分写入下保留所有 256 字节。"""
    server = install_host(monkeypatch)
    server.bridge = True
    payload = bytes(range(256)) * 8
    with rpm.ssh("lab").serial("echo", python_path="python3", newline="\r") as uart:
        result = uart.run(payload, timeout=3)
        assert result.wait(payload, timeout=3).text == payload
        assert uart.kind == "serial"
    assert server.clients[0].closed
    assert all(ch.closed for ch in server.channels)
    assert max(map(len, server.commands)) < 2000


def test_remote_uart_idle_prompt_and_single_newline(monkeypatch):
    """§CORE-06/chain：无换行提示符由远端空闲事件冲刷，newline 只追加一次。"""
    server = install_host(monkeypatch)
    server.bridge = True
    with rpm.ssh("lab") as host:
        with host.serial("echo", python_path="python3") as uart:
            assert uart.run("login: ").wait("login: ", timeout=2).text == "login: "
        with host.serial("echo", python_path="python3", newline="\r") as uart:
            assert uart.run("reboot").wait(b"reboot\r", timeout=2).text == b"reboot\r"
            with pytest.raises(rpm.LogWaitTimeoutError):
                uart.wait(b"\r", timeout=.1)


def test_remote_uart_open_failure_releases_chain(monkeypatch):
    """§CORE-01/chain：READY 前打开串口失败，构造报错并回收宿主。"""
    server = install_host(monkeypatch)
    server.bridge = True
    with pytest.raises(rpm.SessionConnectionError, match="port denied"):
        rpm.ssh("lab").serial("fail-open", python_path="python3")
    assert server.clients[0].closed


def test_remote_uart_reader_failure_reaches_wait(monkeypatch):
    """§CORE-06/chain：桥接 ERROR 唤醒 wait，不退化成匹配超时。"""
    server = install_host(monkeypatch)
    server.bridge = True
    with rpm.ssh("lab") as host:
        with host.serial("fail-read", python_path="python3") as uart:
            with pytest.raises(rpm.SessionConnectionError, match="unplugged"):
                uart.wait(b"not arriving", timeout=2)


def test_upstream_disconnect_wakes_device_wait(monkeypatch):
    """§CORE-06/chain：上游断开向终点传播，并唤醒无输出的等待。"""
    server = install_host(monkeypatch)
    server.bridge = True
    with rpm.ssh("lab") as host:
        with host.serial("echo", python_path="python3") as uart:
            server.active = False
            with pytest.raises(rpm.SessionConnectionError, match="disconnected"):
                uart.wait("never", timeout=2)


def test_wait_detects_overwritten_text_cursor():
    """§CORE-06/chain：超过日志容量的历史游标必须报错，不能静默漏掉输出。"""
    with rpm.local() as session:
        result = session.run(sys.executable, "-c", "print('line\\n'*11000)")
        with pytest.raises(rpm.SessionError, match="overrun"):
            result.wait("line", timeout=.1)


def test_wait_detects_overwritten_byte_cursor(monkeypatch):
    """§CORE-06/chain：UART 超过字节容量必须报告 overrun。"""
    port = install_pyserial_stub(monkeypatch)
    port.echo = True
    with rpm.serial("COM_TEST") as uart:
        result = uart.run(b"x" * (1024 * 1024 + 1))
        with pytest.raises(rpm.SessionError, match="overrun"):
            result.wait(b"x", timeout=.2)


def test_remote_uart_client_lease_releases_port(monkeypatch):
    """§CORE-01/chain：客户端失联后，远端租约到期主动关闭桥接和串口。"""
    wire = install_host(monkeypatch)
    wire.bridge = True
    with rpm.ssh("lab").serial("echo", python_path="python3", connect_timeout=1) as uart:
        wire.drop_writes = True
        with pytest.raises(rpm.SessionConnectionError, match="lease|closed|ended"):
            uart.wait(b"never", timeout=7)
        bridge = next(ch for ch in wire.channels if getattr(ch, "process", None))
        assert bridge.process.wait(timeout=2) != 0


def test_remote_uart_write_timeout_is_not_replayed(monkeypatch):
    """§CORE-02/chain：ACK 超时抛 CommandTimeoutError，不能继续重放不确定写入。"""
    wire = install_host(monkeypatch)
    wire.bridge = True
    with rpm.ssh("lab").serial("echo", python_path="python3") as uart:
        wire.drop_writes = True
        with pytest.raises(rpm.CommandTimeoutError):
            uart.run(b"command", timeout=.1)
        with pytest.raises(rpm.SessionConnectionError):
            uart.run(b"command", timeout=.1)


def test_serial_decode_failure_wakes_wait(monkeypatch):
    """§CORE-06/chain：文本解码阶段故障同样唤醒等待者，不允许读线程悄悄死亡。"""
    port = install_pyserial_stub(monkeypatch)
    with rpm.serial("COM_TEST", encoding="invalid-rpm-codec") as uart:
        port.feed(b"data\n")
        with pytest.raises(rpm.SessionConnectionError, match="encoding"):
            uart.wait("data", timeout=1)


def test_serial_close_reports_cancellation_failure(monkeypatch):
    """§CORE-01/chain：取消读取失败也要释放端口并向调用者报告。"""
    port = install_pyserial_stub(monkeypatch)
    def fail(): raise OSError("cancel failed")
    port.cancel_read = fail
    uart = rpm.serial("COM_TEST")
    with pytest.raises(rpm.SessionConnectionError, match="cancel failed"):
        uart.close()
    assert port._closed
