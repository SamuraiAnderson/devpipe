"""CORE-04 文件传输（doc/core-lib-requirements.md § CORE-04）。

规格映射：
    §CORE-04/copy/within-session           → test_copy_within_local
    §CORE-04/copy/overwrite=False          → test_copy_no_overwrite_keeps_target
    §CORE-04/push/other-to-current         → test_push_from_local_to_fake_remote
    §CORE-04/pull/current-to-other         → test_pull_from_fake_remote_to_local
    §CORE-04/copy/direction/independent    → test_copy_between_local_and_fake_remote
    §CORE-04/push/target-must-be-caller    → test_push_target_must_belong_to_caller
    §CORE-04/pull/source-must-be-caller    → test_pull_source_must_belong_to_caller
    §CORE-04/copy/no-caller                → test_copy_between_remotes_needs_no_caller
    §CORE-04/copy/bare-string              → test_copy_bare_strings_use_default_local
    §CORE-04/copy/path-cwd                 → test_copy_resource_path_uses_session_cwd
    §CORE-04/copy/logs-both-ends           → test_copy_logs_on_both_sessions
    §CORE-04/copy/closed                   → test_copy_closed_session_raises
    §CORE-04/remote-to-remote/via-local    → test_remote_to_remote_via_local_tmp
    §CORE-04/result/fields                 → test_transfer_result_fields
"""

from __future__ import annotations

from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import SessionClosedError, TransferError

from ._helpers.fake_remote import FakeRemoteSession


def _write(p: Path, content: str = "hi") -> None:
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- copy 内部


def test_copy_within_local(local_session, tmp_path: Path):
    """§CORE-04：``copy()`` 在同一会话内部复制文件。"""
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    _write(src, "abc")
    r = rpm.copy(
        local_session.path(str(src)), local_session.path(str(dst))
    )
    assert r.transferred is True
    assert r.bytes_transferred == 3
    assert dst.read_text() == "abc"
    ends = [rec for rec in local_session.logs.records() if rec.event == "transfer_end"]
    assert len(ends) == 1


def test_copy_no_overwrite_keeps_target(local_session, tmp_path: Path):
    """§CORE-04：``overwrite=False`` 时目标存在则跳过。"""
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "new")
    _write(dst, "old")
    r = rpm.copy(
        local_session.path(str(src)),
        local_session.path(str(dst)),
        overwrite=False,
    )
    assert r.transferred is False
    assert dst.read_text() == "old"


# ----------------------------------------------------------- push / pull 方向


def test_push_from_local_to_fake_remote(
    fake_remote: FakeRemoteSession, local_session, tmp_path: Path
):
    """§CORE-04：``push()`` 从其他环境（这里是 local）传入当前会话（fake-remote）。"""
    src = tmp_path / "payload.bin"
    _write(src, "PAYLOAD")
    r = fake_remote.push(
        local_session.path(str(src)),
        fake_remote.path("/data/payload.bin"),
    )
    assert r.transferred is True
    assert fake_remote.read_file("/data/payload.bin") == b"PAYLOAD"


def test_pull_from_fake_remote_to_local(
    fake_remote: FakeRemoteSession, local_session, tmp_path: Path
):
    """§CORE-04：``pull()`` 从当前会话（fake-remote）传出到本地。"""
    fake_remote.put_file("/data/payload.bin", b"PULL-PAYLOAD")
    dst = tmp_path / "out.bin"
    r = fake_remote.pull(
        fake_remote.path("/data/payload.bin"),
        local_session.path(str(dst)),
    )
    assert r.transferred is True
    assert dst.read_bytes() == b"PULL-PAYLOAD"


def test_copy_between_local_and_fake_remote(
    fake_remote: FakeRemoteSession, local_session, tmp_path: Path
):
    """§CORE-04：``rpm.copy()`` 跨会话复制，不需要从某一端发起。"""
    src = tmp_path / "cx.bin"
    _write(src, "CX")
    r = rpm.copy(
        local_session.path(str(src)),
        fake_remote.path("/tmp/cx.bin"),
    )
    assert r.transferred is True
    assert fake_remote.read_file("/tmp/cx.bin") == b"CX"


# -------------------------------------------------------- caller 必须参与


def test_push_target_must_belong_to_caller(
    fake_remote: FakeRemoteSession, local_session, tmp_path: Path
):
    """§CORE-04：``push()`` 的 target 必须属于调用会话。"""
    src = tmp_path / "s.txt"
    _write(src)
    with pytest.raises(TransferError):
        # 用 local 会话调用 push，但 target 却在 fake-remote
        local_session.push(
            local_session.path(str(src)),
            fake_remote.path("/tmp/x"),
        )


def test_pull_source_must_belong_to_caller(
    fake_remote: FakeRemoteSession, local_session, tmp_path: Path
):
    """§CORE-04：``pull()`` 的 source 必须属于调用会话。"""
    fake_remote.put_file("/tmp/x", b"data")
    with pytest.raises(TransferError):
        # 用 local 会话调用 pull，但 source 却在 fake-remote
        local_session.pull(
            fake_remote.path("/tmp/x"),
            local_session.path(str(tmp_path / "out.bin")),
        )


def test_copy_between_remotes_needs_no_caller(fake_remote_factory, local_session):
    """§CORE-04：两个远端之间的 ``rpm.copy()`` 不需要第三方调用会话。"""
    a = fake_remote_factory("a")
    b = fake_remote_factory("b")
    a.put_file("/x", b"data")
    r = rpm.copy(a.path("/x"), b.path("/y"))
    assert r.transferred is True
    assert b.read_file("/y") == b"data"
    assert not hasattr(local_session, "copy")


def test_remote_to_remote_via_local_tmp(fake_remote_factory):
    """§CORE-04：两个远端之间的 copy 由库通过本地临时文件自动中转。"""
    a = fake_remote_factory("a")
    b = fake_remote_factory("b")
    a.put_file("/src", b"RELAY-BYTES")
    r = rpm.copy(a.path("/src"), b.path("/dst"))
    assert r.transferred is True
    assert b.read_file("/dst") == b"RELAY-BYTES"


# -------------------------------------------------------- TransferResult


def test_transfer_result_fields(local_session, tmp_path: Path):
    """§CORE-04：``TransferResult`` 至少包含 source/target/transferred/bytes/duration。"""
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "1234567890")
    r = rpm.copy(
        local_session.path(str(src)), local_session.path(str(dst))
    )
    assert isinstance(r.source, rpm.ResourcePath)
    assert isinstance(r.target, rpm.ResourcePath)
    assert r.transferred is True
    assert r.bytes_transferred == 10
    assert r.duration >= 0.0


def test_copy_bare_strings_use_default_local(tmp_path: Path):
    """§CORE-04：``rpm.copy()`` 的裸字符串是本机路径，不跟随另一个 local 会话的工作目录。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("abc", encoding="utf-8")
    sess = rpm.local(default_cwd=str(workspace))
    try:
        with pytest.raises(TransferError):
            rpm.copy("rpm-copy-bare-not-in-cwd.txt", "rpm-copy-bare-dest.txt")
        src = tmp_path / "abs-src.txt"
        dst = tmp_path / "abs-dst.txt"
        src.write_text("abs", encoding="utf-8")
        result = rpm.copy(str(src), str(dst))
        assert dst.read_text(encoding="utf-8") == "abs"
        assert result.source.session.kind == "local"
        assert result.source.session.root is not sess.root
    finally:
        sess.close()


def test_copy_resource_path_uses_session_cwd(tmp_path: Path):
    """§CORE-04：``sess.path()`` 仍按该会话的工作目录解析。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("abc", encoding="utf-8")
    sess = rpm.local(default_cwd=str(workspace))
    try:
        result = rpm.copy(sess.path("a.txt"), sess.path("b.txt"))
        assert (workspace / "b.txt").read_text(encoding="utf-8") == "abc"
        assert result.source.session.root is sess.root
        assert result.target.session.root is sess.root
    finally:
        sess.close()


def test_copy_logs_on_both_sessions(fake_remote_factory):
    """§CORE-04：跨会话 ``rpm.copy()`` 在源和目标上都记传输日志。"""
    a = fake_remote_factory("a")
    b = fake_remote_factory("b")
    a.put_file("/src", b"LOG")
    mark_a = a.logs.cursor()
    mark_b = b.logs.cursor()
    rpm.copy(a.path("/src"), b.path("/dst"))
    assert any(r.event == "transfer_end" for r in a.logs.records(since=mark_a))
    assert any(r.event == "transfer_end" for r in b.logs.records(since=mark_b))


def test_copy_closed_session_raises(tmp_path: Path):
    """§CORE-04：任一端会话已关闭时 ``rpm.copy()`` 抛 ``SessionClosedError``。"""
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "x")
    sess = rpm.local()
    src_path = sess.path(str(src))
    dst_path = sess.path(str(dst))
    sess.close()
    with pytest.raises(SessionClosedError):
        rpm.copy(src_path, dst_path)
