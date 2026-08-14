"""pytest 通用 fixture。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def tmpdir_path(tmp_path: Path) -> Path:
    """便于类型化访问的 ``tmp_path`` 别名。"""
    return tmp_path


@pytest.fixture
def python_bin() -> str:
    """当前解释器路径，供 subprocess 测试使用（跨平台稳定）。"""
    return sys.executable
