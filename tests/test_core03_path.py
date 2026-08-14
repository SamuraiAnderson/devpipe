"""CORE-03 路径与文件对象（doc/core-lib-requirements.md § CORE-03）。

规格映射：
    §CORE-03/bound-to-session         → test_path_bound_to_session
    §CORE-03/relative/uses-at-view    → test_path_relative_uses_at_view
    §CORE-03/relative/stable-after-at → test_path_stable_after_later_at
    §CORE-03/pathlib-like/exists      → test_path_exists_is_file_is_dir
    §CORE-03/pathlib-like/stat        → test_path_stat_returns_size_and_mtime
    §CORE-03/pathlib-like/stat-missing → test_path_stat_missing_raises_resource_error
    §CORE-03/pathlib-like/remove      → test_path_remove_deletes_file
    §CORE-03/pathlib-like/remove-missing → test_path_remove_missing_ok
    §CORE-03/pathlib-like/mkdir       → test_path_mkdir_with_and_without_parents
    §CORE-03/pathlib-like/name-parent → test_path_name_and_parent
    §CORE-03/absolute-path-ignores-cwd → test_absolute_path_ignores_cwd
    §CORE-03/tilde-expansion          → test_tilde_expansion_uses_home_dir
    §CORE-03/no-hidden-cache/refresh  → test_refresh_is_available_and_returns_none
    §CORE-03/os-fspath                → test_resource_path_is_os_pathlike
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import ResourceError, ResourceNotFoundError


# --------------------------------------------------------------------- 绑定


def test_path_bound_to_session(tmp_workspace: rpm.LocalSession, tmp_path: Path):
    """§CORE-03：``session.path(...)`` 返回的 ``ResourcePath`` 绑定所属会话。"""
    p = tmp_workspace.path("hello.txt")
    assert p.session is tmp_workspace
    assert Path(p.path).resolve() == (tmp_path / "hello.txt").resolve()


def test_path_relative_uses_at_view(tmp_workspace: rpm.LocalSession, tmp_path: Path):
    """§CORE-03：相对路径基于创建它的 ``at()`` 视图解析。"""
    (tmp_path / "sub").mkdir()
    view = tmp_workspace.at("sub")
    p = view.path("x.bin")
    assert Path(p.path).resolve() == (tmp_path / "sub" / "x.bin").resolve()


def test_path_stable_after_later_at(tmp_workspace: rpm.LocalSession, tmp_path: Path):
    """§CORE-03：创建后再新建视图不影响已存在的路径。"""
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    view_a = tmp_workspace.at("one")
    p = view_a.path("a.txt")
    _ = tmp_workspace.at("two")
    assert Path(p.path).resolve() == (tmp_path / "one" / "a.txt").resolve()


# ------------------------------------------------------------- pathlib-like


def test_path_exists_is_file_is_dir(local_session, tmp_path: Path):
    """§CORE-03：``exists`` / ``is_file`` / ``is_dir`` 语义与 ``pathlib`` 一致。"""
    f = tmp_path / "f.txt"
    f.write_text("hi")
    assert local_session.path(str(f)).exists()
    assert local_session.path(str(f)).is_file()
    assert not local_session.path(str(f)).is_dir()
    assert local_session.path(str(tmp_path)).is_dir()
    assert not local_session.path(str(tmp_path / "missing")).exists()


def test_path_stat_returns_size_and_mtime(local_session, tmp_path: Path):
    """§CORE-03：``stat`` 返回 ``ResourceStat``（size/mtime/is_dir）。"""
    f = tmp_path / "f.txt"
    f.write_text("hello")
    st = local_session.path(str(f)).stat()
    assert st.size == 5
    assert st.mtime > 0
    assert st.is_dir is False


def test_path_stat_missing_raises_resource_error(local_session, tmp_path: Path):
    """§CORE-03：不存在时 ``stat`` 必须抛 ``ResourceError``（子类）。"""
    with pytest.raises(ResourceError):
        local_session.path(str(tmp_path / "no-such")).stat()


def test_path_remove_deletes_file(local_session, tmp_path: Path):
    """§CORE-03：``remove()`` 删除已有文件。"""
    f = tmp_path / "f.txt"
    f.write_text("x")
    p = local_session.path(str(f))
    p.remove()
    assert not p.exists()


def test_path_remove_missing_ok(local_session, tmp_path: Path):
    """§CORE-03：``remove(missing_ok=True)`` 不抛异常。"""
    p = local_session.path(str(tmp_path / "ghost"))
    p.remove(missing_ok=True)
    # 默认应抛 ResourceNotFoundError
    with pytest.raises(ResourceNotFoundError):
        p.remove()


def test_path_mkdir_with_and_without_parents(local_session, tmp_path: Path):
    """§CORE-03：``mkdir`` 支持 ``parents`` / ``exist_ok`` 参数。"""
    deep = local_session.path(str(tmp_path / "a" / "b" / "c"))
    with pytest.raises(FileNotFoundError):
        deep.mkdir()  # 不带 parents 无法创建深层目录
    deep.mkdir(parents=True)
    assert deep.is_dir()
    deep.mkdir(parents=True, exist_ok=True)


def test_path_name_and_parent(tmp_workspace: rpm.LocalSession):
    """§CORE-03：``name`` / ``parent`` 属性遵循 pathlib 语义。"""
    p = tmp_workspace.path("a/b/c.txt")
    assert p.name == "c.txt"
    assert p.parent.name == "b"


# ---------------------------------------------------------- 绝对路径 / ~


def test_absolute_path_ignores_cwd(tmp_workspace: rpm.LocalSession, tmp_path: Path):
    """§CORE-03：绝对路径不再拼接 ``at()`` 工作目录。"""
    other = tmp_path / "other"
    other.mkdir()
    p = tmp_workspace.path(str(other))
    assert Path(p.path).resolve() == other.resolve()


def test_tilde_expansion_uses_home_dir(tmp_workspace: rpm.LocalSession):
    """§CORE-03：``~`` 展开为会话的 ``home_dir``（本地为用户家目录）。"""
    # 本地会话在构造时把 home_dir 设为 Path.home()
    p = tmp_workspace.path("~/rpm-tilde-probe.txt")
    home = str(Path.home())
    # 展开后应以家目录为前缀（Windows 上 normpath 会保留大小写）
    assert Path(p.path).parent.resolve() == Path(home).resolve()


# ------------------------------------------------------------ refresh / os


def test_refresh_is_available_and_returns_none(tmp_workspace: rpm.LocalSession):
    """§CORE-03：不使用不可见缓存；``refresh()`` 存在且无返回值。"""
    p = tmp_workspace.path("x")
    assert p.refresh() is None


def test_resource_path_is_os_pathlike(tmp_workspace: rpm.LocalSession):
    """§CORE-03：``ResourcePath`` 支持 ``os.PathLike``（本地场景可安全使用）。"""
    p = tmp_workspace.path("a.txt")
    assert os.fspath(p) == p.path
