"""Specification mapping: §CORE-04/chain → staging, cleanup, deadlines, errors."""
import pytest
import time
import redpymake as rpm
from ._helpers.chain_files import install_files
from ._helpers.chain_host import Reply


@pytest.mark.parametrize("platform,kind", [("posix", "adb"), ("windows", "adb"), ("windows", "wsl")])
def test_chain_transfer_roundtrip(monkeypatch, tmp_path, platform, kind):
    """§CORE-04/chain：跨宿主 push/pull 保留字节并移除远端临时目录。"""
    wire = install_files(monkeypatch, platform, kind)
    source, target = tmp_path / "source.bin", tmp_path / "target.bin"
    source.write_bytes(bytes(range(256)))
    with getattr(rpm.ssh("host"), kind)("device") as device:
        assert device.push(str(source), "/device/payload", timeout=3).transferred
        assert device.pull("/device/payload", str(target), timeout=3).transferred
        assert target.read_bytes() == source.read_bytes()
    assert wire.cleanups == 2 and not wire.files
    assert any(value is not None for value in wire.timeouts)


@pytest.mark.parametrize("failure", ["sftp", "device", "cleanup", "both", "timeout"])
def test_chain_transfer_failure_and_cleanup(monkeypatch, tmp_path, failure):
    """§CORE-04/chain：每阶段失败都回收 staging，原始错误与清理错误分别保留。"""
    wire = install_files(monkeypatch)
    wire.sftp_failure = PermissionError("sftp denied") if failure == "sftp" else None
    wire.device_failure = failure in {"device", "both"}
    wire.cleanup_failure = failure in {"cleanup", "both"}
    wire.delay = .15 if failure == "timeout" else 0
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with rpm.ssh("host").adb("SN") as device:
        with pytest.raises(rpm.RedPyMakeError) as caught:
            device.push(str(source), "/device/payload", timeout=.1 if failure == "timeout" else 3)
    assert wire.cleanups == 1
    if failure == "both":
        assert "device transfer denied" in str(caught.value)
        assert "cleanup" in str(caught.value.cleanup_error)
    if failure == "sftp": assert "sftp denied" in str(caught.value)
    if failure == "timeout": assert not wire.device


def test_sftp_permission_is_not_missing(monkeypatch):
    """§CORE-04/chain：SFTP 权限失败必须抛库异常，不返回路径不存在。"""
    wire = install_files(monkeypatch)
    wire.sftp_failure = PermissionError("denied")
    with rpm.ssh("host") as host:
        with pytest.raises(rpm.RedPyMakeError, match="denied"):
            host.path("/protected").exists()


@pytest.mark.parametrize("kind,platform", [("adb", "posix"), ("wsl", "windows")])
def test_device_path_failure_does_not_report_missing(monkeypatch, kind, platform):
    """§CORE-04/chain：设备探测错误不能返回 False，missing_ok/exist_ok 不能掩盖权限错误。"""
    wire = install_files(monkeypatch, platform, kind)
    with getattr(rpm.ssh("host"), kind)("device") as device:
        wire.handler = lambda command: Reply(rc=1, err=b"device denied")
        for action in (lambda: device.path("/protected").exists(),
                       lambda: device.path("/protected").is_file(),
                       lambda: device.path("/protected").is_dir(),
                       lambda: device.path("/protected").remove(missing_ok=True),
                       lambda: device.path("/protected").mkdir(exist_ok=True)):
            with pytest.raises(rpm.RedPyMakeError, match="device denied"):
                action()


def test_transfer_probe_uses_same_deadline(monkeypatch, tmp_path):
    """§CORE-04/chain：overwrite 探测消耗同一个预算，过期后不能开始上传。"""
    wire = install_files(monkeypatch)
    previous = wire.handler
    def slow_probe(command):
        if "[ -e" in command:
            time.sleep(.15)
            return Reply(b"no")
        return previous(command)
    wire.handler = slow_probe
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with rpm.ssh("host").adb("SN") as device:
        with pytest.raises(rpm.TransferError):
            device.push(str(source), "/device/payload", timeout=.1, overwrite=False)
    assert not wire.device
