"""CORE-10 脚本发现（doc/core-lib-requirements.md § CORE-10）。

规格映射：
    §CORE-10/discovery/pattern-default    → test_discover_matches_rpm_prefix
    §CORE-10/discovery/exclude-underscore → test_discover_excludes_underscore_files
    §CORE-10/discovery/exclude-dirs       → test_discover_excludes_common_dirs
    §CORE-10/discovery/recursive          → test_discover_recurses_subdirectories
    §CORE-10/discovery/stable-order       → test_discover_returns_stable_order
    §CORE-10/ast/no-import                → test_discover_never_imports_target
    §CORE-10/ast/script-block             → test_discover_detects_rpm_script_block
    §CORE-10/ast/no-block                 → test_discover_flags_missing_rpm_script_block
    §CORE-10/ast/script-name-literal      → test_discover_extracts_script_name_literal
    §CORE-10/ast/docstring                → test_discover_extracts_module_docstring
    §CORE-10/ast/factories                → test_discover_detects_factory_calls
    §CORE-10/ast/syntax-error             → test_discover_survives_syntax_error
    §CORE-10/discovery/pyproject          → test_discover_reads_pyproject_patterns
    §CORE-10/discovery/patterns-override  → test_discover_patterns_argument_overrides
"""

from __future__ import annotations

from pathlib import Path

import pytest

import redpymake as rpm


# ------------------------------------------------------------ 基础前缀发现


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_discover_matches_rpm_prefix(tmp_path: Path):
    """§CORE-10/discovery/pattern-default：默认只匹配 rpm_*.py。"""
    _write(tmp_path, "rpm_hello.py", "def main():\n    pass\n")
    _write(tmp_path, "helpers.py", "x = 1\n")
    _write(tmp_path, "test_foo.py", "def test_x():\n    pass\n")
    cards = rpm.discover(tmp_path)
    names = {c.path.name for c in cards}
    assert names == {"rpm_hello.py"}


def test_discover_excludes_underscore_files(tmp_path: Path):
    """§CORE-10/discovery/exclude-underscore：以 _ 开头的 .py 永远不发现。"""
    _write(tmp_path, "rpm_ok.py", "def main():\n    pass\n")
    _write(tmp_path, "_helpers.py", "def main():\n    pass\n")
    cards = rpm.discover(tmp_path)
    assert [c.path.name for c in cards] == ["rpm_ok.py"]


def test_discover_excludes_common_dirs(tmp_path: Path):
    """§CORE-10/discovery/exclude-dirs：.git / __pycache__ / .venv 等硬排除。"""
    for d in [".git", "__pycache__", ".venv", "node_modules", "dist", "build"]:
        _write(tmp_path, f"{d}/rpm_hidden.py", "def main():\n    pass\n")
    _write(tmp_path, "rpm_visible.py", "def main():\n    pass\n")
    cards = rpm.discover(tmp_path)
    assert [c.path.name for c in cards] == ["rpm_visible.py"]


def test_discover_recurses_subdirectories(tmp_path: Path):
    """§CORE-10/discovery/recursive：子目录内的 rpm_*.py 也被发现。"""
    _write(tmp_path, "rpm_a.py", "def main():\n    pass\n")
    _write(tmp_path, "sub/rpm_b.py", "def main():\n    pass\n")
    _write(tmp_path, "sub/deep/rpm_c.py", "def main():\n    pass\n")
    cards = rpm.discover(tmp_path)
    assert sorted(c.path.name for c in cards) == ["rpm_a.py", "rpm_b.py", "rpm_c.py"]


def test_discover_returns_stable_order(tmp_path: Path):
    """§CORE-10/discovery/stable-order：结果按相对路径字典序稳定。"""
    for name in ["rpm_c.py", "rpm_a.py", "rpm_b.py"]:
        _write(tmp_path, name, "def main():\n    pass\n")
    names = [c.path.name for c in rpm.discover(tmp_path)]
    assert names == sorted(names)


# --------------------------------------------------------------- AST 分析


def test_discover_never_imports_target(tmp_path: Path):
    """§CORE-10/ast/no-import：AST 分析不执行目标脚本的顶层代码。

    在脚本顶层写入一个副作用（写文件），发现后该副作用不应出现。
    """
    marker = tmp_path / "SIDE_EFFECT"
    _write(
        tmp_path,
        "rpm_side.py",
        f"""from pathlib import Path
Path({str(marker)!r}).write_text('leaked')

def main() -> None:
    pass
""",
    )
    cards = rpm.discover(tmp_path)
    assert cards and cards[0].path.name == "rpm_side.py"
    assert not marker.exists(), "discover must not execute module top-level"


def test_discover_detects_rpm_script_block(tmp_path: Path):
    """§CORE-10/ast/script-block：识别 with rpm.script(...) 块。"""
    _write(
        tmp_path,
        "rpm_ok.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("hello") as run:
        pass
""",
    )
    (card,) = rpm.discover(tmp_path)
    assert card.has_script_block is True
    assert card.lineno is not None and card.lineno >= 3


def test_discover_flags_missing_rpm_script_block(tmp_path: Path):
    """§CORE-10/ast/no-block：文件匹配前缀但没有 with rpm.script(...) → has_script_block=False。"""
    _write(tmp_path, "rpm_bare.py", "def main() -> None:\n    print('hi')\n")
    (card,) = rpm.discover(tmp_path)
    assert card.has_script_block is False
    assert card.error is None


def test_discover_extracts_script_name_literal(tmp_path: Path):
    """§CORE-10/ast/script-name-literal：从 rpm.script(name="...") 拿字面量名字。"""
    _write(
        tmp_path,
        "rpm_named.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("my-build") as run:
        pass
""",
    )
    (card,) = rpm.discover(tmp_path)
    assert card.script_name == "my-build"


def test_discover_extracts_module_docstring(tmp_path: Path):
    """§CORE-10/ast/docstring：模块首个字符串字面量作为 docstring 元数据。"""
    _write(
        tmp_path,
        "rpm_doc.py",
        '''"""这个脚本演示如何 X。

多行说明的第二段。
"""
def main() -> None:
    pass
''',
    )
    (card,) = rpm.discover(tmp_path)
    assert card.docstring is not None
    assert "演示如何 X" in card.docstring


def test_discover_detects_factory_calls(tmp_path: Path):
    """§CORE-10/ast/factories：识别 rpm.local() / rpm.wsl() 等工厂调用。"""
    _write(
        tmp_path,
        "rpm_multi.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("m") as run:
        with rpm.local() as here:
            here.run("echo", "hi")
        with rpm.wsl() as sh:
            sh.run("uname")
""",
    )
    (card,) = rpm.discover(tmp_path)
    assert set(card.factories) == {"local", "wsl"}


def test_discover_survives_syntax_error(tmp_path: Path):
    """§CORE-10/ast/syntax-error：语法错误文件不使整个 discover 崩溃。"""
    _write(tmp_path, "rpm_ok.py", "def main() -> None:\n    pass\n")
    _write(tmp_path, "rpm_broken.py", "def main( \n")
    cards = rpm.discover(tmp_path)
    by_name = {c.path.name: c for c in cards}
    assert set(by_name) == {"rpm_ok.py", "rpm_broken.py"}
    assert by_name["rpm_broken.py"].error is not None
    assert by_name["rpm_broken.py"].has_script_block is False


# --------------------------------------------------------------- 配置读取


def test_discover_reads_pyproject_patterns(tmp_path: Path):
    """§CORE-10/discovery/pyproject：pyproject.toml 覆盖默认模式。"""
    _write(
        tmp_path,
        "pyproject.toml",
        """[tool.redpymake.discovery]
patterns = ["make_*.py"]
""",
    )
    _write(tmp_path, "make_x.py", "def main():\n    pass\n")
    _write(tmp_path, "rpm_x.py", "def main():\n    pass\n")
    cards = rpm.discover(tmp_path)
    assert [c.path.name for c in cards] == ["make_x.py"]


def test_discover_patterns_argument_overrides(tmp_path: Path):
    """§CORE-10/discovery/patterns-override：显式 patterns= 优先于 pyproject 与默认。"""
    _write(
        tmp_path,
        "pyproject.toml",
        """[tool.redpymake.discovery]
patterns = ["make_*.py"]
""",
    )
    _write(tmp_path, "make_x.py", "def main():\n    pass\n")
    _write(tmp_path, "rpm_x.py", "def main():\n    pass\n")
    _write(tmp_path, "run_x.py", "def main():\n    pass\n")
    cards = rpm.discover(tmp_path, patterns=["run_*.py"])
    assert [c.path.name for c in cards] == ["run_x.py"]


# --------------------------------------------------------------- 类型导出


def test_script_card_type_exposed():
    """§CORE-10/factory：ScriptCard 类型可从 redpymake 顶层导入。"""
    assert isinstance(rpm.ScriptCard, type)
