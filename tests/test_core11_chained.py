"""Specification mapping: §CORE-11/chain → chain pooling, identity and teardown."""
import pytest
import redpymake as rpm
from ._helpers.chain_host import install_host


def test_workspace_pools_chained_devices_by_host(monkeypatch, tmp_path):
    """§CORE-11/chain：同宿主同设备复用，不同宿主隔离，退出关闭所有资源。"""
    wire = install_host(monkeypatch)
    with rpm.workspace(tmp_path) as ws:
        with rpm.ssh("one").adb("SN") as first:
            first.run("echo", "hello")
        again = rpm.ssh("one").adb("SN")
        other = rpm.ssh("two").adb("SN")
        assert first.root is again.root
        assert first.session_id != other.session_id
        assert not first.closed
        assert len(ws.sessions()) == 4
    assert all(session.closed for session in (first, again, other))
    assert all(client.closed for client in wire.clients)


def test_workspace_chained_view_and_conflict(monkeypatch, tmp_path):
    """§CORE-11/chain：cwd 保持视图，同 key 参数冲突必须明确失败。"""
    install_host(monkeypatch)
    with rpm.workspace(tmp_path):
        one = rpm.ssh("lab").adb("SN", default_cwd="/a")
        two = rpm.ssh("lab").adb("SN", default_cwd="/b")
        assert one.root is two.root
        assert one.path("x").path == "/a/x"
        assert two.path("x").path == "/b/x"
        with pytest.raises(ValueError, match="conflict"):
            rpm.ssh("lab").adb("SN", adb_path="other-adb")
