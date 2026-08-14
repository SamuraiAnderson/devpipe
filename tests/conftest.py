"""pytest 通用 fixture。

用途：
- ``python_bin`` / ``python_probe``：跨平台稳定地跑一小段 Python 代码。
- ``local_session``：现成的 ``LocalSession``，自动 close。
- ``tmp_workspace``：绑到 ``tmp_path`` 的 ``LocalSession``，方便"当前目录"
  语义的测试。
- ``fake_remote`` / ``fake_remote_factory``：``FakeRemoteSession`` fixture，
  用于跨会话传输测试。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterator

import pytest

import redpymake as rpm

from ._helpers.fake_remote import FakeRemoteSession


@pytest.fixture
def tmpdir_path(tmp_path: Path) -> Path:
    """便于类型化访问的 ``tmp_path`` 别名。"""
    return tmp_path


@pytest.fixture
def python_bin() -> str:
    """当前解释器路径，供 subprocess 测试使用（跨平台稳定）。"""
    return sys.executable


@pytest.fixture
def python_probe(python_bin: str) -> Callable[[str], list[str]]:
    """把 ``python -c "<src>"`` 打包成可直接传给 ``run(*args)`` 的 argv 列表。

    用法::

        argv = python_probe("print('hi')")
        sess.run(*argv)
    """

    def _make(source: str) -> list[str]:
        return [python_bin, "-c", source]

    return _make


@pytest.fixture
def local_session() -> Iterator[rpm.LocalSession]:
    """现成的本地会话；测试结束自动 close。"""
    sess = rpm.local()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Iterator[rpm.LocalSession]:
    """绑定到 ``tmp_path`` 的本地会话。"""
    sess = rpm.local(default_cwd=str(tmp_path))
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def fake_remote() -> Iterator[FakeRemoteSession]:
    """默认的 ``FakeRemoteSession``；测试结束自动 close。"""
    sess = FakeRemoteSession()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def fake_remote_factory() -> Iterator[Callable[..., FakeRemoteSession]]:
    """需要多个假远端时使用；返回创建函数，所有产物在结束后统一 close。"""
    created: list[FakeRemoteSession] = []

    def _make(label: str = "fake-remote") -> FakeRemoteSession:
        sess = FakeRemoteSession(label=label)
        created.append(sess)
        return sess

    try:
        yield _make
    finally:
        for sess in created:
            sess.close()
