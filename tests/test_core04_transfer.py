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
    §CORE-04/failure/surfaces-stderr/wsl-pull   → test_wsl_pull_failure_surfaces_stderr
    §CORE-04/failure/surfaces-stderr/wsl-push   → test_wsl_push_failure_surfaces_stderr
    §CORE-04/failure/surfaces-stderr/wsl-copy   → test_wsl_in_distro_copy_failure_surfaces_stderr
    §CORE-04/failure/surfaces-stderr/wsl-mkdir  → test_wsl_mkdir_failure_surfaces_stderr
    §CORE-04/failure/surfaces-stderr/adb-mkdir  → test_adb_mkdir_failure_surfaces_stderr
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


# ---------------------------------------------- 失败消息必须透传底层 stderr
#
# 这些用例覆盖的核心契约：
#   当某个会话实现为了跑 cp / stat / rm / mkdir 等 POSIX 工具而内部"壳调用"
#   （WSL 走 ``_wsl_shell``、ADB 走 ``_shell_probe``）时，一旦底层返回非零，
#   失败分支必须把 ``stderr`` 写进 ``TransferError.message`` + ``TransferError.stderr``；
#   不得只回传 ``stdout``（它通常是空的），让异常退化成 "wsl pull failed:" 这种
#   不说原因的消息。project-overview.mdc："捕获后若没有在同一个函数里重新抛
#   出，就是掩盖失败"——丢诊断信息是同类问题的一种。


def _wsl_fake_run(stderr_msg: str, *, home: str | None = "/root"):
    """构造一个假 ``_run_wsl``：对家目录探测放行，其余命令统一返回 ``(1, "", stderr_msg)``。"""

    def _impl(_self, extra, *, timeout=None, stdin=None):
        joined = " ".join(extra)
        if home is not None and 'printf %s "$HOME"' in joined:
            return 0, home, ""
        return 1, "", stderr_msg

    return _impl


def test_wsl_pull_failure_surfaces_stderr(
    monkeypatch, local_session, tmp_path: Path
):
    """§CORE-04/failure/surfaces-stderr/wsl-pull：
    ``pull()`` 走 WSL 的 ``cp`` 失败时，``TransferError`` 必须携带 stderr。
    """
    sess = rpm.wsl(wsl_path="wsl-not-used")
    monkeypatch.setattr(
        type(sess),
        "_run_wsl",
        _wsl_fake_run("cp: cannot stat '/no/such': No such file or directory"),
    )
    try:
        with pytest.raises(TransferError) as ei:
            sess.pull(
                sess.path("/no/such"),
                local_session.path(str(tmp_path / "out.bin")),
            )
    finally:
        sess.close()
    msg = str(ei.value)
    assert "No such file or directory" in msg, (
        f"stderr was swallowed; message={msg!r}"
    )
    assert "cp:" in msg
    assert ei.value.stderr and "No such file" in ei.value.stderr
    assert ei.value.returncode == 1


def test_wsl_push_failure_surfaces_stderr(
    monkeypatch, local_session, tmp_path: Path
):
    """§CORE-04/failure/surfaces-stderr/wsl-push：
    ``push()`` 走 WSL 的 ``cp`` 失败时，``TransferError`` 必须携带 stderr。
    """
    src = tmp_path / "s.bin"
    src.write_bytes(b"x")
    sess = rpm.wsl(wsl_path="wsl-not-used")
    monkeypatch.setattr(
        type(sess),
        "_run_wsl",
        _wsl_fake_run(
            "cp: cannot create regular file '/ro/x': Read-only file system"
        ),
    )
    try:
        with pytest.raises(TransferError) as ei:
            sess.push(local_session.path(str(src)), sess.path("/ro/x"))
    finally:
        sess.close()
    msg = str(ei.value)
    assert "Read-only file system" in msg, (
        f"stderr was swallowed; message={msg!r}"
    )
    assert ei.value.stderr and "Read-only file system" in ei.value.stderr
    assert ei.value.returncode == 1


def test_wsl_in_distro_copy_failure_surfaces_stderr(monkeypatch):
    """§CORE-04/failure/surfaces-stderr/wsl-copy：
    同一 WSL 会话内 ``copy()`` 失败时，``TransferError`` 必须携带 stderr。
    """
    sess = rpm.wsl(wsl_path="wsl-not-used")
    monkeypatch.setattr(
        type(sess),
        "_run_wsl",
        _wsl_fake_run("cp: cannot stat '/a': No such file or directory"),
    )
    try:
        with pytest.raises(TransferError) as ei:
            rpm.copy(sess.path("/a"), sess.path("/b"))
    finally:
        sess.close()
    msg = str(ei.value)
    assert "No such file or directory" in msg, (
        f"stderr was swallowed; message={msg!r}"
    )
    assert ei.value.stderr


def test_wsl_mkdir_failure_surfaces_stderr(monkeypatch):
    """§CORE-04/failure/surfaces-stderr/wsl-mkdir：
    ``ResourcePath.mkdir()`` 走 WSL 的 ``mkdir`` 失败时，``TransferError``
    必须携带 stderr。
    """
    sess = rpm.wsl(wsl_path="wsl-not-used")
    monkeypatch.setattr(
        type(sess),
        "_run_wsl",
        _wsl_fake_run(
            "mkdir: cannot create directory '/ro/x': Read-only file system"
        ),
    )
    try:
        with pytest.raises(TransferError) as ei:
            sess.path("/ro/x").mkdir()
    finally:
        sess.close()
    msg = str(ei.value)
    assert "Read-only file system" in msg, (
        f"stderr was swallowed; message={msg!r}"
    )
    assert ei.value.stderr and "Read-only file system" in ei.value.stderr


def test_adb_mkdir_failure_surfaces_stderr(monkeypatch):
    """§CORE-04/failure/surfaces-stderr/adb-mkdir：
    ADB 的 ``_shell_probe`` 对接 ``mkdir`` 失败时，必须把 stderr 透出来，
    不得只留 ``"adb mkdir failed:"`` 这种空消息。
    """
    from redpymake._adb import AdbSession

    def _fake(_self, extra, *, timeout=None, stdin=None):
        if extra and extra[0] == "get-state":
            return 0, "device\n", ""
        # 任何 shell 调用都返回失败 + stderr
        return 1, "", (
            "mkdir: '/ro/x': Read-only file system"
        )

    monkeypatch.setattr(AdbSession, "_run_adb", _fake)
    sess = rpm.adb(adb_path="adb-not-used")
    try:
        with pytest.raises(TransferError) as ei:
            sess.path("/ro/x").mkdir()
    finally:
        sess.close()
    msg = str(ei.value)
    assert "Read-only file system" in msg, (
        f"stderr was swallowed; message={msg!r}"
    )
    assert ei.value.stderr and "Read-only file system" in ei.value.stderr
