"""§CORE-01/chain: ownership, host detection, endpoint identity and construction.

Specification mapping: CORE-01/chain → every public factory/close test below.
"""
import pytest
import sys
import redpymake as rpm
from ._helpers.chain_host import install_host, Reply


def test_chain_endpoint_owns_unscoped_ssh(monkeypatch):
    """§CORE-01/chain：独占链终点释放上游，保留独立类型和日志。"""
    wire = install_host(monkeypatch)
    host = rpm.ssh("lab")
    android = host.adb("SN")
    assert android.kind == "adb"
    assert android.session_id != host.session_id
    assert android.logs is not host.logs
    assert android.root is android
    android.close()
    android.close()
    assert host.closed and wire.clients[0].closed


def test_scoped_host_survives_child_close_and_failure(monkeypatch):
    """§CORE-01/chain：作用域持有的宿主可共享，子失败不关闭宿主。"""
    wire = install_host(monkeypatch)
    with rpm.ssh("lab") as host:
        first = host.adb("one")
        second = host.adb("two")
        first.close()
        assert not host.closed and not second.closed
        wire.fail_adb = True
        with pytest.raises(rpm.SessionConnectionError):
            host.adb("offline")
        assert not host.closed
    assert second.closed


def test_failed_exclusive_chain_releases_host(monkeypatch):
    """§CORE-01/chain：第二步构造失败回收独占 SSH。"""
    wire = install_host(monkeypatch)
    wire.fail_adb = True
    with pytest.raises(rpm.SessionConnectionError):
        rpm.ssh("lab").adb("offline")
    assert wire.clients[0].closed


def test_chain_from_view_does_not_inherit_host_cwd(monkeypatch):
    """§CORE-01/chain：at 只提供宿主连接，cwd 仍归终点。"""
    install_host(monkeypatch)
    with rpm.ssh("lab") as host:
        with host.at("/host-only").adb("SN") as device:
            assert device.path("x").path == "/x"
            assert device.at("/device").path("x").path == "/device/x"


def test_unsupported_route_is_explicit():
    """§CORE-01/chain：未支持的组合明确报 UnsupportedOperationError。"""
    with rpm.local() as local:
        with pytest.raises(rpm.UnsupportedOperationError):
            local.adb("SN")


def test_windows_detection_and_lazy_wsl(monkeypatch):
    """§CORE-01/chain：自动检测 Windows，WSL 构造不启动发行版。"""
    wire = install_host(monkeypatch, "windows")
    with rpm.ssh("win") as host:
        assert host.path_style == "windows"
        wire.commands.clear()
        with host.wsl("Ubuntu") as linux:
            assert linux.kind == "wsl" and linux.path_style == "posix"
            assert linux.home_dir is None
        assert not any("-d Ubuntu" in command for command in wire.commands)


def test_detection_does_not_guess_after_failed_probes(monkeypatch):
    """§CORE-01/chain：检测失败必须报错并关闭 SSH。"""
    wire = install_host(monkeypatch, "unknown")
    with pytest.raises(rpm.SessionConnectionError):
        rpm.ssh("unknown")
    assert wire.clients[0].closed


def test_second_exclusive_branch_requires_scope(monkeypatch):
    """§CORE-01/chain：独占转移后不能隐式再分支。"""
    install_host(monkeypatch)
    host = rpm.ssh("lab")
    with host.adb("one"):
        with pytest.raises(rpm.SessionError):
            host.adb("two")


def test_nested_scopes_keep_parent_alive(monkeypatch):
    """§CORE-01/chain：宿主嵌套作用域在最外层退出时释放。"""
    install_host(monkeypatch)
    with rpm.ssh("lab") as host:
        with host:
            with host.adb("one"):
                pass
        assert not host.closed
        host.run("true")


def test_close_failure_keeps_primary_error(monkeypatch):
    """§CORE-01/chain：异常退出中的清理错误不能覆盖业务错误。"""
    wire = install_host(monkeypatch)
    def fail_close(): raise OSError("cleanup failed")
    with pytest.raises(ValueError, match="primary") as caught:
        with rpm.ssh("lab").adb("SN"):
            wire.clients[0].close = fail_close
            raise ValueError("primary")
    assert "cleanup" in str(caught.value.cleanup_error)


def test_serial_optional_dependency_error_stays_explicit(monkeypatch):
    """§CORE-01/chain：远端 UART 不影响本地可选依赖错误类型。"""
    monkeypatch.setitem(sys.modules, "serial", None)
    with pytest.raises(rpm.UnsupportedOperationError, match="pyserial"):
        rpm.serial("COM_TEST")


def test_remote_wsl_custom_executable_is_validated(monkeypatch):
    """§CORE-01/chain：指定远端 wsl_path 也校验存在性，仍不启动发行版。"""
    wire = install_host(monkeypatch, "windows")
    wire.handler = lambda command: Reply(rc=1, err=b"missing wsl") if "Get-Command" in command else None
    with pytest.raises(rpm.SessionConnectionError, match="missing wsl"):
        rpm.ssh("windows").wsl("Ubuntu", wsl_path="C:\\missing\\wsl.exe")
    assert wire.clients[0].closed
