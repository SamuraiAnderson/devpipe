"""Real chained endpoints. Explicit RPM_TEST_CHAIN_* opt-in; no device discovery."""
import os
import uuid
import pytest
import redpymake as rpm

pytestmark = pytest.mark.integration


def test_ssh_adb_run_and_transfer(ssh_target, tmp_path):
    """§CORE-01/chain、§CORE-04/chain：真实 SSH → ADB 命令及文件往返。"""
    serial = os.environ.get("RPM_TEST_CHAIN_ADB_SERIAL")
    if not serial:
        pytest.skip("set RPM_TEST_CHAIN_ADB_SERIAL on the SSH host")
    data = bytes(range(256)) * 8
    source, target = tmp_path / "source.bin", tmp_path / "target.bin"
    source.write_bytes(data)
    remote = "/data/local/tmp/redpymake-" + uuid.uuid4().hex
    with rpm.ssh(**ssh_target).adb(serial) as android:
        assert android.run("echo", "chain-ready", timeout=10).stdout == "chain-ready"
        try:
            android.push(str(source), remote, timeout=10)
            android.pull(remote, str(target), timeout=10)
            assert target.read_bytes() == data
        finally:
            android.path(remote).remove(missing_ok=True)


def test_ssh_wsl_run_and_transfer(ssh_target, tmp_path):
    """§CORE-01/chain、§CORE-04/chain：真实 Windows SSH → WSL 及 wslpath 中转。"""
    distro = os.environ.get("RPM_TEST_CHAIN_WSL_DISTRO")
    if not distro:
        pytest.skip("set RPM_TEST_CHAIN_WSL_DISTRO on a Windows SSH host")
    source, target = tmp_path / "source.bin", tmp_path / "target.bin"
    source.write_bytes(bytes(range(256)))
    remote = "/tmp/redpymake-" + uuid.uuid4().hex
    with rpm.ssh(**ssh_target).wsl(distro) as linux:
        assert linux.run("printf", "%s", "chain-ready", timeout=10).stdout == "chain-ready"
        try:
            linux.push(str(source), remote, timeout=10)
            linux.pull(remote, str(target), timeout=10)
            assert target.read_bytes() == source.read_bytes()
        finally:
            linux.path(remote).remove(missing_ok=True)


def test_ssh_serial_explicit_loopback(ssh_target):
    """§CORE-06/chain：只有显式指定回环串口才发送二进制验收数据。"""
    port = os.environ.get("RPM_TEST_CHAIN_SERIAL_LOOPBACK_PORT")
    if not port:
        pytest.skip("set RPM_TEST_CHAIN_SERIAL_LOOPBACK_PORT for a wired UART loopback")
    with rpm.ssh(**ssh_target).serial(port,
            python_path=os.environ.get("RPM_TEST_CHAIN_PYTHON")) as uart:
        payload = bytes(range(256)) * 8
        assert uart.run(payload, timeout=5).wait(payload, timeout=5).text == payload
