"""CORE-10 命名目标（doc/core-lib-requirements.md § CORE-10）。

规格映射：
    §CORE-10/targets/local-run              → test_target_opens_local_session
    §CORE-10/targets/at-prefix              → test_at_prefix_names_the_same_target
    §CORE-10/targets/key-merge              → test_local_file_overrides_keys_not_the_whole_target
    §CORE-10/targets/password-hidden        → test_listing_omits_resolved_password
    §CORE-10/targets/password-env-missing   → test_unset_password_env_raises
    §CORE-10/targets/override-password      → test_override_password_skips_env
    §CORE-10/targets/reject-password        → test_literal_password_rejected_in_both_files
    §CORE-10/targets/expand-key             → test_key_filename_expands_user
    §CORE-10/targets/unknown-name           → test_unknown_target_lists_available_names
    §CORE-10/targets/bad-kind               → test_illegal_kind_raises
    §CORE-10/targets/unknown-key            → test_unknown_key_raises
    §CORE-10/targets/missing-required       → test_missing_factory_key_raises
    §CORE-10/targets/illegal-name           → test_illegal_target_name_raises
    §CORE-10/targets/override-cwd           → test_override_wins_over_config
    §CORE-10/targets/walk-limit             → test_pyproject_search_stops_after_six_directories
    §CORE-10/targets/logs-not-root          → test_runtime_log_dir_does_not_hide_parent_config
    §CORE-10/targets/same-root              → test_targets_toml_outside_pyproject_root_is_ignored
    §CORE-10/targets/toml-only              → test_targets_toml_alone_is_a_project_root
    §CORE-10/targets/sorted                 → test_targets_are_sorted_by_name
    §CORE-10/targets/syntax                 → test_broken_toml_raises
    §CORE-10/targets/workspace-borrow       → test_target_reuses_workspace_session
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake._targets import list_targets, resolve_target


_LOCAL = """\
[tool.redpymake.targets.box]
kind = "local"
"""


def _pyproject(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def _local_file(root: Path, body: str) -> None:
    dest = root / ".redpymake"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "targets.toml").write_text(body, encoding="utf-8")


def _down(base: Path, levels: int) -> Path:
    cur = base
    for i in range(levels):
        cur = cur / f"d{i}"
        cur.mkdir(exist_ok=True)
    return cur


def test_target_opens_local_session(tmp_path: Path, python_probe):
    """§CORE-10/targets/local-run：kind=local 的目标能真正跑命令。"""
    _pyproject(tmp_path, _LOCAL)
    with rpm.target("box", root=tmp_path) as sess:
        res = sess.run(*python_probe("print('FROM-TARGET')"))
    assert "FROM-TARGET" in res.stdout


def test_at_prefix_names_the_same_target(tmp_path: Path, python_probe):
    """§CORE-10/targets/at-prefix：``@box`` 与 ``box`` 是同一个目标。"""
    _pyproject(tmp_path, _LOCAL)
    with rpm.target("@box", root=tmp_path) as sess:
        res = sess.run(*python_probe("print('AT')"))
    assert "AT" in res.stdout


def test_local_file_overrides_keys_not_the_whole_target(tmp_path: Path, monkeypatch):
    """§CORE-10/targets/key-merge：本机文件只覆盖写出的键，kind/user 仍来自 pyproject。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
user = "root"
default_cwd = "/workspace"
password_env = "BOARD_PASSWORD"
""",
    )
    _local_file(
        tmp_path,
        """
[board]
host = "10.0.0.8"
""",
    )
    monkeypatch.setenv("BOARD_PASSWORD", "s3cret")
    infos = list_targets(tmp_path)
    assert len(infos) == 1
    info = infos[0]
    assert info.kind == "ssh"
    assert info.source == ("pyproject", ".redpymake/targets.toml")
    assert info.params["host"] == "10.0.0.8"
    assert info.params["user"] == "root"
    assert info.params["default_cwd"] == "/workspace"
    assert info.params["password_env"] == "BOARD_PASSWORD"
    resolved = resolve_target("board", root=tmp_path)
    assert resolved.params["password"] == "s3cret"
    assert resolved.params["user"] == "root"
    assert "password_env" not in resolved.params


def test_listing_omits_resolved_password(tmp_path: Path, monkeypatch):
    """§CORE-10/targets/password-hidden：列出目标时不带解析后的口令。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
host = "10.0.0.8"
password_env = "BOARD_PASSWORD"
""",
    )
    monkeypatch.setenv("BOARD_PASSWORD", "s3cret")
    info = list_targets(tmp_path)[0]
    assert "password" not in info.params
    assert "s3cret" not in repr(info)


def test_unset_password_env_raises(tmp_path: Path, monkeypatch):
    """§CORE-10/targets/password-env-missing：建连时变量未设置即失败；只列出则不要求。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
host = "10.0.0.8"
password_env = "BOARD_PASSWORD"
""",
    )
    monkeypatch.delenv("BOARD_PASSWORD", raising=False)
    assert list_targets(tmp_path)[0].params["password_env"] == "BOARD_PASSWORD"
    with pytest.raises(rpm.TargetConfigError) as ei:
        resolve_target("board", root=tmp_path)
    assert "BOARD_PASSWORD" in str(ei.value)
    assert ei.value.name == "board"


def test_override_password_skips_env(tmp_path: Path, monkeypatch):
    """§CORE-10/targets/override-password：overrides 里的 password 不再去读环境变量。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
host = "10.0.0.8"
password_env = "BOARD_PASSWORD_MISSING"
""",
    )
    monkeypatch.delenv("BOARD_PASSWORD_MISSING", raising=False)
    resolved = resolve_target("board", root=tmp_path, password="from-call")
    assert resolved.params["password"] == "from-call"


def test_literal_password_rejected_in_both_files(tmp_path: Path):
    """§CORE-10/targets/reject-password：两份配置文件里的字面量 password 都拒绝，且不回显。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
host = "10.0.0.8"
password = "supersecret-py"
""",
    )
    with pytest.raises(rpm.TargetConfigError) as ei:
        list_targets(tmp_path)
    assert "supersecret-py" not in str(ei.value)
    assert ei.value.name == "board"

    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
host = "10.0.0.8"
""",
    )
    _local_file(
        tmp_path,
        """
[board]
password = "supersecret-local"
""",
    )
    with pytest.raises(rpm.TargetConfigError) as ei:
        resolve_target("board", root=tmp_path)
    assert "supersecret-local" not in str(ei.value)


def test_key_filename_expands_user(tmp_path: Path, monkeypatch):
    """§CORE-10/targets/expand-key：key_filename 的 ``~`` 按本机家目录展开。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
host = "10.0.0.8"
key_filename = "~/.ssh/board"
""",
    )
    resolved = resolve_target("board", root=tmp_path)
    assert os.path.normpath(resolved.params["key_filename"]) == os.path.normpath(
        os.path.join(str(tmp_path), ".ssh", "board")
    )


def test_unknown_target_lists_available_names(tmp_path: Path):
    """§CORE-10/targets/unknown-name：未知目标名的消息带上已有名字。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.box]
kind = "local"

[tool.redpymake.targets.other]
kind = "local"
""",
    )
    with pytest.raises(rpm.TargetConfigError) as ei:
        rpm.target("missing", root=tmp_path)
    text = str(ei.value)
    assert "box" in text
    assert "other" in text
    assert ei.value.name == "missing"


def test_illegal_kind_raises(tmp_path: Path):
    """§CORE-10/targets/bad-kind：kind 不在工厂集合里。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.box]
kind = "telnet"
""",
    )
    with pytest.raises(rpm.TargetConfigError) as ei:
        rpm.target("box", root=tmp_path)
    assert "telnet" in str(ei.value) or "kind" in str(ei.value)


def test_unknown_key_raises(tmp_path: Path):
    """§CORE-10/targets/unknown-key：工厂签名里没有的键。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.box]
kind = "local"
nope = 1
""",
    )
    with pytest.raises(rpm.TargetConfigError) as ei:
        list_targets(tmp_path)
    assert "nope" in str(ei.value)


def test_missing_factory_key_raises(tmp_path: Path):
    """§CORE-10/targets/missing-required：ssh 缺 host。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.board]
kind = "ssh"
user = "root"
""",
    )
    with pytest.raises(rpm.TargetConfigError) as ei:
        list_targets(tmp_path)
    assert "host" in str(ei.value)


def test_illegal_target_name_raises(tmp_path: Path):
    """§CORE-10/targets/illegal-name：名字含空白。"""
    _pyproject(tmp_path, _LOCAL)
    with pytest.raises(rpm.TargetConfigError) as ei:
        rpm.target("bad name", root=tmp_path)
    assert ei.value.name == "bad name"


def test_override_wins_over_config(tmp_path: Path, python_probe):
    """§CORE-10/targets/override-cwd：调用点 overrides 压过配置里的 default_cwd。"""
    configured = tmp_path / "configured"
    override = tmp_path / "override"
    configured.mkdir()
    override.mkdir()
    _pyproject(
        tmp_path,
        f"""
[tool.redpymake.targets.box]
kind = "local"
default_cwd = { _toml(configured) }
""",
    )
    with rpm.target("box", root=tmp_path, default_cwd=str(override)) as sess:
        res = sess.run(*python_probe("import os; print(os.getcwd())"))
    assert Path(res.stdout.strip()).resolve() == override.resolve()


def test_pyproject_search_stops_after_six_directories(tmp_path: Path):
    """§CORE-10/targets/walk-limit：起点算第 1 层，第 6 层还看得到，再往上就看不到。"""
    _pyproject(tmp_path, _LOCAL)
    assert [item.name for item in list_targets(_down(tmp_path, 5))] == ["box"]
    assert list_targets(_down(tmp_path, 6)) == []


def test_runtime_log_dir_does_not_hide_parent_config(tmp_path: Path):
    """§CORE-10/targets/logs-not-root：只有日志的 .redpymake/ 不挡住上层 pyproject。"""
    _pyproject(tmp_path, _LOCAL)
    child = tmp_path / "scripts"
    (child / ".redpymake" / "logs").mkdir(parents=True)
    assert [item.name for item in list_targets(child)] == ["box"]


def test_targets_toml_outside_pyproject_root_is_ignored(tmp_path: Path):
    """§CORE-10/targets/same-root：两份配置都从 pyproject 所在目录读，子目录里的另一份不算。"""
    _pyproject(tmp_path, _LOCAL)
    child = tmp_path / "sub"
    child.mkdir()
    _local_file(
        child,
        """
[box]
kind = "ssh"
host = "10.9.9.9"
""",
    )
    info = list_targets(child)[0]
    assert info.kind == "local"
    assert info.source == ("pyproject",)


def test_targets_toml_alone_is_a_project_root(tmp_path: Path):
    """§CORE-10/targets/toml-only：没有 pyproject 时，targets.toml 自己可以当项目根。"""
    child = tmp_path / "sub"
    child.mkdir()
    _local_file(
        child,
        """
[box]
kind = "local"
""",
    )
    info = list_targets(child)[0]
    assert info.kind == "local"
    assert info.source == (".redpymake/targets.toml",)


def test_targets_are_sorted_by_name(tmp_path: Path):
    """§CORE-10/targets/sorted：列出顺序按名字排序。"""
    _pyproject(
        tmp_path,
        """
[tool.redpymake.targets.b]
kind = "local"

[tool.redpymake.targets.a]
kind = "local"
""",
    )
    assert [item.name for item in list_targets(tmp_path)] == ["a", "b"]


def test_broken_toml_raises(tmp_path: Path):
    """§CORE-10/targets/syntax：配置解析失败是 TargetConfigError，不是裸异常。"""
    _pyproject(tmp_path, "[[[not toml\n")
    with pytest.raises(rpm.TargetConfigError) as ei:
        list_targets(tmp_path)
    assert ei.value.source is not None


def test_target_reuses_workspace_session(tmp_path: Path, python_probe):
    """§CORE-10/targets/workspace-borrow：workspace 作用域内 target() 借出池里的会话。"""
    _pyproject(tmp_path, _LOCAL)
    with rpm.workspace(tmp_path) as ws:
        first = ws.local()
        borrowed = rpm.target("box", root=tmp_path)
        assert borrowed.root is first.root
        with borrowed:
            borrowed.run(*python_probe("print('pooled')"))
        assert first.root.closed is False


def _toml(path: Path) -> str:
    text = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
