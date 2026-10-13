"""Specification mapping:
§CORE-04/tftp/lifecycle → ready port, cleanup and exceptional exit.
§CORE-04/tftp/read-only → RRQ only, regular files and confined paths.
§CORE-04/tftp/completion/failure/timeout → ACK, errors, bounded retries.
§CORE-08/lazy-extras → import without tftpy; actionable missing dependency.
"""
from concurrent.futures import ThreadPoolExecutor
import socket
import struct
import subprocess
import sys
import threading

import pytest
import redpymake as rpm


@pytest.fixture
def tftp_root(tmp_path):
    pytest.importorskip("tftpy")
    root = tmp_path / "中文 payloads"
    root.mkdir()
    return root


def _request(sock, server, name, *, opcode=1):
    sock.sendto(struct.pack("!H", opcode) + name.encode("ascii") + b"\0octet\0",
                (server.host, server.port))
    return sock.recvfrom(65535)


def _download(server, name):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        packet, address = _request(sock, server, name)
        data = bytearray()
        block = 1
        while True:
            opcode, number = struct.unpack("!HH", packet[:4])
            assert (opcode, number) == (3, block), packet
            data.extend(packet[4:])
            sock.sendto(struct.pack("!HH", 4, number), address)
            if len(packet) < 516:
                return bytes(data)
            block += 1
            packet, address = sock.recvfrom(65535)


@pytest.mark.parametrize("size", [0, 512, 2049])
def test_tftp_real_download_and_cleanup(tftp_root, size):
    """§CORE-04/tftp/lifecycle/completion：真实 UDP 下载及最后 ACK 后可释放端口。"""
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    (tftp_root / "firmware.bin").write_bytes(payload)
    with rpm.tftpd(tftp_root, port=0) as server:
        assert not isinstance(server, rpm.Session)
        assert server.host == "127.0.0.1" and server.port > 0
        assert server.root_dir == tftp_root.resolve()
        assert _download(server, "firmware.bin") == payload
        assert server.wait("firmware.bin", timeout=3) is None
        server.check()
        completed = [r for r in server.logs.records() if r.event == "transfer_end"]
        assert completed[-1].fields["bytes_transferred"] == size
        address = (server.host, server.port)
    server.close()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(address)


def test_tftp_exceptional_exit_releases_resources(tftp_root):
    """§CORE-04/tftp/lifecycle：正文异常原样传播且监听 socket 被关闭。"""
    before = {t.ident for t in threading.enumerate()}
    with pytest.raises(ValueError, match="body failure"):
        with rpm.tftpd(tftp_root, port=0) as server:
            address = (server.host, server.port)
            raise ValueError("body failure")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(address)
    assert not [t for t in threading.enumerate()
                if t.ident not in before and t.name.startswith("rpm-tftp")]


def test_tftp_bind_failure_has_cause(tftp_root):
    """§CORE-04/tftp/failure：端口已占用时进入上下文即报错，保留原因。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        with pytest.raises(rpm.TransferError) as failure:
            with rpm.tftpd(tftp_root, port=occupied.getsockname()[1]):
                pytest.fail("occupied port was accepted")
    assert isinstance(failure.value.__cause__, OSError)


def test_tftp_missing_root_fails_before_serving(tmp_path):
    """§CORE-04/tftp/lifecycle：不存在的根目录不能创建成功的服务。"""
    with pytest.raises(rpm.TransferError):
        with rpm.tftpd(tmp_path / "missing", port=0):
            pytest.fail("missing root was accepted")


@pytest.mark.parametrize("name,opcode,errorcode", [
    ("missing.bin", 1, 1), ("../secret.bin", 1, 2),
    ("/absolute.bin", 1, 2), ("C:/secret.bin", 1, 2),
    (r"..\secret.bin", 1, 2), ("upload.bin", 2, 2),
])
def test_tftp_rejection_is_reported_to_peer_and_caller(tftp_root, name, opcode, errorcode):
    """§CORE-04/tftp/read-only/failure：请求拒绝通过协议与统一异常双向可见。"""
    (tftp_root.parent / "secret.bin").write_bytes(b"private")
    with pytest.raises(rpm.TransferError) as failure:
        with rpm.tftpd(tftp_root, port=0) as server:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                packet, _ = _request(sock, server, name, opcode=opcode)
                assert struct.unpack("!HH", packet[:4]) == (5, errorcode)
            server.wait(name, timeout=3)
    assert failure.value.__cause__ is not None
    assert not (tftp_root / "upload.bin").exists()


def test_tftp_symlink_escape_is_rejected(tftp_root):
    """§CORE-04/tftp/read-only：链接解析到根目录外时不得读取文件。"""
    outside = tftp_root.parent / "secret.bin"
    outside.write_bytes(b"private")
    try:
        (tftp_root / "link.bin").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(rpm.TransferError):
        with rpm.tftpd(tftp_root, port=0) as server:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                packet, _ = _request(sock, server, "link.bin")
                assert struct.unpack("!HH", packet[:4]) == (5, 2)
            server.wait("link.bin", timeout=3)


def test_tftp_ack_timeout_and_retry_are_visible(tftp_root):
    """§CORE-04/tftp/timeout：不发 ACK 时有界重传，最终失败唤醒等待者。"""
    (tftp_root / "payload.bin").write_bytes(b"payload")
    with pytest.raises(rpm.TransferError, match="(?i)tim.*out"):
        with rpm.tftpd(tftp_root, port=0, timeout=0.1, retries=1) as server:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                first, peer = _request(sock, server, "payload.bin")
                second, second_peer = sock.recvfrom(65535)
                assert (first, peer) == (second, second_peer)
                server.wait("payload.bin", timeout=3)
    assert any(r.event == "transfer_retry" for r in server.logs.records())


def test_tftp_wait_timeout_without_client(tftp_root):
    """§CORE-04/tftp/timeout：尚无客户端时 wait 超时不能伪装完成。"""
    with rpm.tftpd(tftp_root, port=0) as server:
        with pytest.raises(rpm.TransferError, match="(?i)tim.*out"):
            server.wait("payload.bin", timeout=0.05)
        server.check()


def test_tftp_wait_requires_final_ack(tftp_root):
    """§CORE-04/tftp/completion：已发出末块仍须等待客户端最后 ACK。"""
    (tftp_root / "payload.bin").write_bytes(b"payload")
    with rpm.tftpd(tftp_root, port=0) as server:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            packet, peer = _request(sock, server, "payload.bin")
            assert packet[:4] == struct.pack("!HH", 3, 1)
            with pytest.raises(rpm.TransferError, match="(?i)tim.*out"):
                server.wait("payload.bin", timeout=0.05)
            sock.sendto(struct.pack("!HH", 4, 1), peer)
            server.wait("payload.bin", timeout=3)


def test_tftp_concurrent_clients_with_options(tftp_root, tmp_path):
    """§CORE-04/tftp/completion：多个客户端独立协商块大小并得到完整文件。"""
    import tftpy

    payload = bytes(range(256)) * 32
    names = ["first.bin", "second.bin"]
    for name in names:
        (tftp_root / name).write_bytes(payload)
    with rpm.tftpd(tftp_root, port=0) as server:
        def download(name):
            client = tftpy.TftpClient(server.host, server.port,
                                     options={"blksize": 1024, "tsize": 0}, flock=False)
            client.download(name, str(tmp_path / name), timeout=1, retries=2)
            return (tmp_path / name).read_bytes()

        with ThreadPoolExecutor(max_workers=2) as clients:
            assert list(clients.map(download, names)) == [payload, payload]
        for name in names:
            server.wait(name, timeout=3)


def test_tftp_close_releases_incomplete_transfer(tftp_root):
    """§CORE-04/tftp/lifecycle：主动关闭也释放尚未 ACK 的传输 socket。"""
    (tftp_root / "payload.bin").write_bytes(b"payload")
    before = {t.ident for t in threading.enumerate()}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        with rpm.tftpd(tftp_root, port=0) as server:
            _, transfer_address = _request(sock, server, "payload.bin")
            listener_address = (server.host, server.port)
    for address in [listener_address, transfer_address]:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(address)
    assert not [t for t in threading.enumerate()
                if t.ident not in before and t.name.startswith("rpm-tftp")]


def test_tftp_missing_extra_is_actionable(monkeypatch, tmp_path):
    """§CORE-08/lazy-extras：缺少 TFTP 依赖用库异常提示安装 extra。"""
    monkeypatch.setitem(sys.modules, "tftpy", None)
    with pytest.raises(rpm.UnsupportedOperationError, match=r"redpymake\[tftp\]"):
        with rpm.tftpd(tmp_path, port=0):
            pytest.fail("missing dependency was accepted")


def test_tftp_public_import_does_not_load_optional_dependency():
    """§CORE-08/lazy-extras：访问工厂和类型不导入 tftpy。"""
    code = "import sys,redpymake as rpm; assert callable(rpm.tftpd); assert isinstance(rpm.TftpServer,type); assert 'tftpy' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
