"""CORE-04 文件传输（doc/core-lib-requirements.md § CORE-04）。

规格映射：
    §CORE-04/copy/within-session           → test_copy_within_local
    §CORE-04/copy/overwrite=False          → test_copy_no_overwrite_keeps_target
    §CORE-04/push/other-to-current         → test_push_from_local_to_fake_remote
    §CORE-04/pull/current-to-other         → test_pull_from_fake_remote_to_local
    §CORE-04/copy/direction/independent    → test_copy_between_local_and_fake_remote
    §CORE-04/push/target-must-be-caller    → test_push_target_must_belong_to_caller
    §CORE-04/pull/source-must-be-caller    → test_pull_source_must_belong_to_caller
    §CORE-04/copy/caller-must-participate  → test_copy_caller_must_be_source_or_target
    §CORE-04/remote-to-remote/via-local    → test_remote_to_remote_via_local_tmp
    §CORE-04/result/fields                 → test_transfer_result_fields
"""

from __future__ import annotations

from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import TransferError

from ._helpers.fake_remote import FakeRemoteSession


def _write(p: Path, content: str = "hi") -> None:
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- copy 内部


def test_copy_within_local(local_session, tmp_path: Path):
    """§CORE-04：``copy()`` 在同一会话内部复制文件。"""
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    _write(src, "abc")
    r = local_session.copy(
        local_session.path(str(src)), local_session.path(str(dst))
    )
    assert r.transferred is True
    assert r.bytes_transferred == 3
    assert dst.read_text() == "abc"


def test_copy_no_overwrite_keeps_target(local_session, tmp_path: Path):
    """§CORE-04：``overwrite=False`` 时目标存在则跳过。"""
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "new")
    _write(dst, "old")
    r = local_session.copy(
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
    """§CORE-04：``copy()`` 跨会话复制；caller 可以是源或目标。"""
    src = tmp_path / "cx.bin"
    _write(src, "CX")
    # caller 是 target 会话
    r = fake_remote.copy(
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


def test_copy_caller_must_be_source_or_target(
    fake_remote_factory, local_session, tmp_path: Path
):
    """§CORE-04：``copy()`` 的调用会话必须是源或目标之一，不能作无关第三方。"""
    a = fake_remote_factory("a")
    b = fake_remote_factory("b")
    a.put_file("/x", b"data")
    with pytest.raises(TransferError):
        local_session.copy(a.path("/x"), b.path("/y"))


# ---------------------------------------------------- 远端到远端本地中转


def test_remote_to_remote_via_local_tmp(fake_remote_factory):
    """§CORE-04：两个远端之间的 copy 由库通过本地临时文件自动中转。"""
    a = fake_remote_factory("a")
    b = fake_remote_factory("b")
    a.put_file("/src", b"RELAY-BYTES")
    # caller 是 source 会话
    r = a.copy(a.path("/src"), b.path("/dst"))
    assert r.transferred is True
    assert b.read_file("/dst") == b"RELAY-BYTES"


# -------------------------------------------------------- TransferResult


def test_transfer_result_fields(local_session, tmp_path: Path):
    """§CORE-04：``TransferResult`` 至少包含 source/target/transferred/bytes/duration。"""
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "1234567890")
    r = local_session.copy(
        local_session.path(str(src)), local_session.path(str(dst))
    )
    assert isinstance(r.source, rpm.ResourcePath)
    assert isinstance(r.target, rpm.ResourcePath)
    assert r.transferred is True
    assert r.bytes_transferred == 10
    assert r.duration >= 0.0
