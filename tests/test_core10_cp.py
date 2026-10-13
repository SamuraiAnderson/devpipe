"""CORE-10 CLI 文件复制契约。

规格映射（doc/core-lib-requirements.md §CORE-10/cli/cp）：
    路径 / --root → test_cp_local_paths, test_cp_named_target_cwd
    方向 / 生命周期 → test_cp_transfer_directions, test_cp_second_connection_failure
    文件 / 目录 → test_cp_directory_destination, test_cp_directory_source
    覆盖 / 跳过 → test_cp_no_clobber
    超时 → test_cp_timeout_forwarded, test_cp_invalid_timeout
    用法 → test_cp_invalid_endpoint, test_cp_missing_operand, test_cp_literal_paths
    失败 / 退出码 → test_cp_missing_source, test_cp_unknown_target,
                    test_cp_transfer_failure, test_cp_connect_failure
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake._cli import main


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "redpymake", "cp", *args],
        cwd=cwd, capture_output=True, text=True, timeout=25,
    )


def test_cp_local_paths(tmp_path):
    """§CORE-10/cli/cp：本机路径按进程 cwd 解析，--root 不改变基准。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("invalid TOML [", encoding="utf-8")
    payload = bytes(range(256))
    (tmp_path / "source.bin").write_bytes(payload)
    (tmp_path / "dest.bin").write_bytes(b"old")
    result = _run_cli("--root", str(project), "source.bin", "dest.bin", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert (tmp_path / "dest.bin").read_bytes() == payload


@pytest.mark.parametrize("direction", ["upload", "download", "within", "between"])
def test_cp_named_target_cwd(tmp_path, direction):
    """§CORE-10/cli/cp：CLI 真正加载命名目标，目标相对路径基于 default_cwd。"""
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    config = "\n".join(
        f'[tool.redpymake.targets.{name}]\nkind = "local"\n'
        f'default_cwd = {json.dumps(str(tmp_path / name))}\n'
        for name in ("one", "two")
    )
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    endpoints = {
        "upload": (tmp_path, tmp_path / "one", "src.bin", "@one:dst.bin"),
        "download": (tmp_path / "one", tmp_path, "@one:src.bin", "dst.bin"),
        "within": (tmp_path / "one", tmp_path / "one", "@one:src.bin", "@one:dst.bin"),
        "between": (tmp_path / "one", tmp_path / "two", "@one:src.bin", "@two:dst.bin"),
    }
    src_dir, dst_dir, source, dest = endpoints[direction]
    (src_dir / "src.bin").write_bytes(b"named-target")
    result = _run_cli(source, dest, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (dst_dir / "dst.bin").read_bytes() == b"named-target"


@pytest.mark.parametrize("direction", ["upload", "download", "within", "between"])
def test_cp_transfer_directions(tmp_path, monkeypatch, fake_remote_factory, direction):
    """§CORE-10/cli/cp：实际远端传输分派 / 中转，同名目标只打开一次且关闭。"""
    sessions = {name: fake_remote_factory(name) for name in ("one", "two")}
    opened = []

    def open_target(name, *, root):
        assert root == str(tmp_path)
        opened.append(name)
        return sessions[name]

    monkeypatch.setattr("redpymake._cli.target", open_target)
    local_src = tmp_path / "src.bin"
    local_dst = tmp_path / "dst.bin"
    local_src.write_bytes(b"payload")
    sessions["one"].put_file("/root/src: a.bin", b"payload")
    args = {
        "upload": (str(local_src), "@one:~/dst.bin"),
        "download": ("@one:~/src: a.bin", str(local_dst)),
        "within": ("@one:~/src: a.bin", "@one:~/dst.bin"),
        "between": ("@one:~/src: a.bin", "@two:~/dst.bin"),
    }[direction]
    assert main(["cp", "--root", str(tmp_path), *args]) == 0
    if direction == "download":
        assert local_dst.read_bytes() == b"payload"
    else:
        dest = "two" if direction == "between" else "one"
        assert sessions[dest].read_file("/root/dst.bin") == b"payload"
    assert opened == (["one", "two"] if direction == "between" else ["one"])
    assert all(sessions[name].closed for name in opened)


@pytest.mark.parametrize("flag", ["-n", "--no-clobber"])
def test_cp_no_clobber(tmp_path, flag):
    """§CORE-10/cli/cp：不覆盖选项保留原文件，跳过也成功。"""
    (tmp_path / "src").write_bytes(b"new")
    (tmp_path / "dst").write_bytes(b"old")
    result = _run_cli(flag, "src", "dst", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dst").read_bytes() == b"old"


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("filename", ["source.bin", "~literal.bin"])
def test_cp_directory_destination(tmp_path, monkeypatch, fake_remote, remote, filename):
    """§CORE-10/cli/cp：目标是现存目录时自动追加源文件名，再应用不覆盖选项。"""
    src = tmp_path / filename
    src.write_bytes(b"first")
    if remote:
        fake_remote.path("/out").mkdir()
        monkeypatch.setattr("redpymake._cli.target", lambda *a, **kw: fake_remote.at("/"))
        dest = "@box:/out"
        read = lambda: fake_remote.read_file(f"/out/{filename}")
    else:
        (tmp_path / "out").mkdir()
        dest = str(tmp_path / "out")
        read = (tmp_path / "out" / filename).read_bytes
    assert main(["cp", str(src), dest]) == 0
    assert read() == b"first"
    # at() 视图的 close 不关闭根连接，允许下一次 CLI 独立打开。
    src.write_bytes(b"second")
    assert main(["cp", "-n", str(src), dest]) == 0
    assert read() == b"first"


def test_cp_directory_source(tmp_path):
    """§CORE-10/cli/cp：目录源没有递归语义，退出 69 且不创建目标。"""
    (tmp_path / "src").mkdir()
    result = _run_cli("src", "dst", cwd=tmp_path)
    assert result.returncode == 69, result.stderr
    assert result.stderr.startswith("redpymake:")
    assert not (tmp_path / "dst").exists()


@pytest.mark.parametrize("filename", ["./@source", "-source", "file with spaces; $x.txt"])
def test_cp_literal_paths(tmp_path, filename):
    """§CORE-10/cli/cp：显式本机 @ 文件、-- 后的短横线及 shell 字符保持字面意义。"""
    (tmp_path / filename).write_bytes(b"literal")
    result = _run_cli("--", filename, str(tmp_path / "dest"), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dest").read_bytes() == b"literal"


@pytest.mark.parametrize("operand", ["", "@", "@box", "@box:", "@:file", "@bad name:file", "@@box:file"])
@pytest.mark.parametrize("side", [0, 1])
def test_cp_invalid_endpoint(tmp_path, monkeypatch, operand, side):
    """§CORE-10/cli/cp：两端语法都在任何建连前验证，用法错误退出 64。"""
    def unexpected_connection(*args, **kwargs):
        pytest.fail("invalid endpoints must not open connections")

    monkeypatch.setattr("redpymake._cli.target", unexpected_connection)
    endpoints = ["@box:src", "@box:dst"]
    endpoints[side] = operand
    with pytest.raises(SystemExit) as error:
        main(["cp", *endpoints])
    assert error.value.code == 64


def test_cp_missing_operand(tmp_path):
    """§CORE-10/cli/cp：缺少源或目标是用法错误。"""
    result = _run_cli("source", cwd=tmp_path)
    assert result.returncode == 64
    assert "redpymake:" in result.stderr


def test_cp_missing_source(tmp_path):
    """§CORE-10/cli/cp：源文件不存在退出 66，无 traceback。"""
    result = _run_cli("missing", "dest", cwd=tmp_path)
    assert result.returncode == 66, result.stderr
    assert result.stderr.startswith("redpymake:")
    assert "missing" in result.stderr
    assert "Traceback" not in result.stderr


def test_cp_unknown_target(tmp_path):
    """§CORE-10/cli/cp：未知命名目标退出 78，复用目标配置诊断。"""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.redpymake.targets.box]\nkind = "local"\n', encoding="utf-8",
    )
    result = _run_cli("@missing:src", "dest", cwd=tmp_path)
    assert result.returncode == 78, result.stderr
    assert result.stderr.startswith("redpymake:")
    assert "box" in result.stderr


@pytest.mark.parametrize("error", [rpm.SessionConnectionError("offline"), rpm.UnsupportedOperationError("no extra")])
def test_cp_connect_failure(tmp_path, monkeypatch, capsys, error):
    """§CORE-10/cli/cp：建连失败或缺少依赖退出 69 并保留诊断。"""
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("redpymake._cli.target", fail)
    assert main(["cp", "@box:src", str(tmp_path / "dst")]) == 69
    assert capsys.readouterr().err == f"redpymake: {error}\n"


def test_cp_second_connection_failure(monkeypatch, fake_remote, capsys):
    """§CORE-10/cli/cp：第二个目标建连失败也释放第一个会话。"""
    def open_target(name, **kwargs):
        if name == "one":
            return fake_remote
        raise rpm.SessionConnectionError("second target offline")

    monkeypatch.setattr("redpymake._cli.target", open_target)
    assert main(["cp", "@one:/src", "@two:/dst"]) == 69
    assert fake_remote.closed
    assert capsys.readouterr().err.startswith("redpymake:")


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf"])
def test_cp_invalid_timeout(tmp_path, timeout):
    """§CORE-10/cli/cp：超时必须是有限正数，非法值退出 64。"""
    result = _run_cli(f"--timeout={timeout}", "src", "dst", cwd=tmp_path)
    assert result.returncode == 64
    assert "redpymake:" in result.stderr


def test_cp_timeout_forwarded(tmp_path, monkeypatch, fake_remote):
    """§CORE-10/cli/cp：指定超时传递到实际传输后端。"""
    source = tmp_path / "src"
    source.write_bytes(b"timeout")
    copy_into = fake_remote._do_copy_into
    observed = []

    def record_timeout(src, dst, *, overwrite, timeout):
        observed.append(timeout)
        return copy_into(src, dst, overwrite=overwrite, timeout=timeout)

    monkeypatch.setattr(fake_remote, "_do_copy_into", record_timeout)
    monkeypatch.setattr("redpymake._cli.target", lambda *a, **kw: fake_remote)
    assert main(["cp", "--timeout", "1.25", str(source), "@box:/dst"]) == 0
    assert observed == [1.25]
    assert fake_remote.read_file("/dst") == b"timeout"
    assert fake_remote.closed


@pytest.mark.parametrize("error, code", [
    (PermissionError("permission denied"), 74),
    (rpm.TransferError("device copy failed: read-only filesystem"), 74),
    (rpm.CommandTimeoutError("transfer deadline expired"), 124),
    (TimeoutError("socket timed out"), 124),
    (rpm.UnsupportedOperationError("copy unsupported"), 69),
    (rpm.SessionConnectionError("connection lost"), 69),
    (rpm.ResourceNotFoundError("source disappeared"), 66),
    (rpm.TransferError("SFTP failed", cause=TimeoutError("timed out")), 124),
])
def test_cp_transfer_failure(tmp_path, monkeypatch, fake_remote, capsys, error, code):
    """§CORE-10/cli/cp：真实传输层包裹的异常映射退出码，保留诊断并关闭连接。"""
    source = tmp_path / "src"
    source.write_bytes(b"failure")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(fake_remote, "_do_copy_into", fail)
    monkeypatch.setattr("redpymake._cli.target", lambda *a, **kw: fake_remote)
    assert main(["cp", str(source), "@box:/dst"]) == code
    stderr = capsys.readouterr().err
    assert stderr.startswith("redpymake:")
    assert str(error) in stderr
    assert "Traceback" not in stderr
    assert fake_remote.closed
