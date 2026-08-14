"""ResourcePath 语义 (CORE-03)。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import ResourceNotFoundError


def test_path_bound_to_session(tmp_path):
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        p = sess.path("hello.txt")
        assert p.session is sess
        assert os.path.normpath(p.path) == os.path.normpath(str(tmp_path / "hello.txt"))


def test_path_relative_uses_at_view(tmp_path):
    (tmp_path / "sub").mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        view = sess.at("sub")
        p = view.path("x.bin")
        assert os.path.normpath(p.path) == os.path.normpath(str(tmp_path / "sub" / "x.bin"))


def test_path_not_affected_by_later_at(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        view_a = sess.at("one")
        p = view_a.path("a.txt")
        # 创建后再切目录，p 不应受影响
        _ = sess.at("two")
        assert os.path.normpath(p.path) == os.path.normpath(str(tmp_path / "one" / "a.txt"))


def test_path_exists_isfile_isdir(tmp_path):
    file = tmp_path / "f.txt"
    file.write_text("hi")
    with rpm.local() as sess:
        p_file = sess.path(str(file))
        p_dir = sess.path(str(tmp_path))
        p_none = sess.path(str(tmp_path / "no-such"))
        assert p_file.exists()
        assert p_file.is_file()
        assert not p_file.is_dir()
        assert p_dir.exists()
        assert p_dir.is_dir()
        assert not p_none.exists()


def test_path_stat_and_remove(tmp_path):
    file = tmp_path / "f.txt"
    file.write_text("hello")
    with rpm.local() as sess:
        p = sess.path(str(file))
        st = p.stat()
        assert st.size == 5
        assert not st.is_dir
        p.remove()
        assert not p.exists()


def test_path_stat_missing(tmp_path):
    with rpm.local() as sess:
        p = sess.path(str(tmp_path / "nope"))
        with pytest.raises(ResourceNotFoundError):
            p.stat()


def test_path_mkdir(tmp_path):
    with rpm.local() as sess:
        p = sess.path(str(tmp_path / "a" / "b" / "c"))
        with pytest.raises(FileNotFoundError):
            p.mkdir()  # 不带 parents 无法创建深层
        p.mkdir(parents=True)
        assert p.is_dir()
        # exist_ok
        p.mkdir(parents=True, exist_ok=True)


def test_name_and_parent(tmp_path):
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        p = sess.path("a/b/c.txt")
        assert p.name == "c.txt"
        assert p.parent.name == "b"


def test_absolute_path_ignores_cwd(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        p = sess.path(str(other))
        assert os.path.normpath(p.path) == os.path.normpath(str(other))
