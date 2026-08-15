"""脚本发现 (CORE-10)。

对目录做递归扫描，找出所有匹配 ``rpm_*.py``（或用户覆盖的 pattern）的
文件，然后**用 AST 分析**抽取元数据（不 import 目标脚本）：

- 模块 docstring
- 是否含 ``with rpm.script(...) as ...:`` 块及其行号
- ``rpm.script(name=...)`` 的字面量参数（若为常量字符串）
- 静态可推的 factory 调用集合（``local`` / ``ssh`` / ``adb`` / ``serial`` / ``wsl``）

`patterns` / `exclude` 的优先级：显式参数 > ``pyproject.toml`` > 默认值。

任何 Python 语法错误的文件都不会使 ``discover(...)`` 崩溃，而是以
``ScriptCard(has_script_block=False, error="...")`` 的形式回填到结果里。
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_diag_logger = logging.getLogger("redpymake")

_DEFAULT_PATTERNS: tuple[str, ...] = ("rpm_*.py",)
_DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
)

_KNOWN_FACTORIES: frozenset[str] = frozenset({"local", "ssh", "adb", "serial", "wsl"})


@dataclass(frozen=True)
class ScriptCard:
    """一个可被 UI / CLI 消费的脚本元数据。

    Attributes:
        path: 脚本绝对路径。
        module_name: 相对 ``root`` 的 dotted 模块名（用 ``/`` 转 ``.`` 并去后缀）。
        script_name: 若 ``rpm.script(name="...")`` 是字面量字符串，则为该字符串；
            否则为 ``None``。
        docstring: 模块首个字符串字面量（模块 docstring）。
        factories: 静态推断的 factory 名列表。
        has_script_block: 是否在模块中找到 ``with rpm.script(...):`` 块。
        lineno: 上述 ``with`` 块出现的行号（``has_script_block=True`` 时非空）。
        error: 语法错误等降级信息；正常时为 ``None``。
    """

    path: Path
    module_name: str
    script_name: str | None
    docstring: str | None
    factories: tuple[str, ...]
    has_script_block: bool
    lineno: int | None
    error: str | None = None

    def to_dict(self) -> dict:
        """便于 CLI --json / Web API 序列化的字典形式。"""
        return {
            "path": str(self.path),
            "module_name": self.module_name,
            "script_name": self.script_name,
            "docstring": self.docstring,
            "factories": list(self.factories),
            "has_script_block": self.has_script_block,
            "lineno": self.lineno,
            "error": self.error,
        }


# ---------------------------------------------------------------------- 公共 API


def discover(
    root: str | os.PathLike[str] = ".",
    *,
    patterns: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
) -> list[ScriptCard]:
    """扫描 ``root`` 目录（递归），返回按路径字典序稳定排列的 ``ScriptCard`` 列表。

    - ``patterns=None``：优先读 ``pyproject.toml`` 的 ``[tool.redpymake.discovery] patterns``，
      找不到则退化到 ``("rpm_*.py",)``。
    - ``exclude=None``：同上；退化到 ``_DEFAULT_EXCLUDES``。
    - 以 ``_`` 开头的 ``.py`` 文件（如 ``_helpers.py``）恒不发现。
    """
    root_path = Path(os.fspath(root)).resolve()

    cfg_patterns, cfg_excludes = _read_pyproject_config(root_path)
    if patterns is None:
        eff_patterns = tuple(cfg_patterns) if cfg_patterns else _DEFAULT_PATTERNS
    else:
        eff_patterns = tuple(patterns)

    if exclude is None:
        eff_excludes = tuple(cfg_excludes) if cfg_excludes else _DEFAULT_EXCLUDES
    else:
        eff_excludes = tuple(exclude)

    excludes_set = set(eff_excludes)

    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        # 就地修剪 dirnames，让 os.walk 不进入被排除目录
        dirnames[:] = [d for d in dirnames if d not in excludes_set]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            if name.startswith("_"):
                continue
            if not any(fnmatch.fnmatch(name, pat) for pat in eff_patterns):
                continue
            hits.append(Path(dirpath) / name)

    # 按相对路径的 posix 字典序稳定
    hits.sort(key=lambda p: p.relative_to(root_path).as_posix())

    cards: list[ScriptCard] = []
    for path in hits:
        cards.append(_analyze_file(path, root_path))
    return cards


# ---------------------------------------------------------------------- 内部


def _read_pyproject_config(root: Path) -> tuple[list[str] | None, list[str] | None]:
    """从 ``root`` 或最近向上找 pyproject.toml 中读 discovery 配置。

    找不到配置或读取失败时返回 ``(None, None)``。
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            return None, None

    candidate = root
    # 从 root 起向上找最多 6 层
    for _ in range(6):
        py_path = candidate / "pyproject.toml"
        if py_path.is_file():
            try:
                with py_path.open("rb") as fp:
                    data = tomllib.load(fp)
            except Exception:  # pragma: no cover - 配置解析失败退化到默认
                _diag_logger.exception("failed to parse %s", py_path)
                return None, None
            cfg = (
                data.get("tool", {})
                .get("redpymake", {})
                .get("discovery", {})
            )
            patterns = cfg.get("patterns")
            excludes = cfg.get("exclude")
            if isinstance(patterns, list) or isinstance(excludes, list):
                return (
                    list(patterns) if isinstance(patterns, list) else None,
                    list(excludes) if isinstance(excludes, list) else None,
                )
            return None, None
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return None, None


def _analyze_file(path: Path, root: Path) -> ScriptCard:
    """对单个 ``.py`` 文件做 AST 分析。语法错误时降级返回 ``error!=None`` 的卡片。"""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _degraded_card(path, root, error=f"read_error: {exc}")

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return _degraded_card(path, root, error=f"syntax_error: {exc.msg} (line {exc.lineno})")

    docstring = ast.get_docstring(tree)
    finder = _ScriptBlockFinder()
    finder.visit(tree)

    return ScriptCard(
        path=path,
        module_name=_module_name(path, root),
        script_name=finder.script_name,
        docstring=docstring,
        factories=tuple(sorted(finder.factories)),
        has_script_block=finder.has_block,
        lineno=finder.lineno,
        error=None,
    )


def _degraded_card(path: Path, root: Path, *, error: str) -> ScriptCard:
    return ScriptCard(
        path=path,
        module_name=_module_name(path, root),
        script_name=None,
        docstring=None,
        factories=(),
        has_script_block=False,
        lineno=None,
        error=error,
    )


def _module_name(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    parts = list(rel.with_suffix("").parts)
    # 去掉可能的空段
    parts = [p for p in parts if p]
    return ".".join(parts) if parts else path.stem


class _ScriptBlockFinder(ast.NodeVisitor):
    """在 AST 里找 ``with rpm.script(...)`` 块与 factory 调用。"""

    def __init__(self) -> None:
        self.has_block: bool = False
        self.lineno: int | None = None
        self.script_name: str | None = None
        self.factories: set[str] = set()

    def _is_rpm_call(self, node: ast.AST, expected_name: str | None = None) -> str | None:
        """如果 ``node`` 是 ``redpymake.<attr>(...)`` / ``rpm.<attr>(...)`` 形式的调用，
        返回 ``attr``；否则返回 None。若 ``expected_name`` 给定则只在匹配时返回。
        """
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        value = func.value
        if not isinstance(value, ast.Name):
            return None
        if value.id not in {"rpm", "redpymake"}:
            return None
        attr = func.attr
        if expected_name is not None and attr != expected_name:
            return None
        return attr

    def visit_With(self, node: ast.With) -> None:  # noqa: N802 (AST hook name)
        for item in node.items:
            attr = self._is_rpm_call(item.context_expr, expected_name="script")
            if attr is not None:
                self.has_block = True
                if self.lineno is None:
                    self.lineno = getattr(node, "lineno", None)
                self._extract_script_name(item.context_expr)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        for item in node.items:
            attr = self._is_rpm_call(item.context_expr, expected_name="script")
            if attr is not None:
                self.has_block = True
                if self.lineno is None:
                    self.lineno = getattr(node, "lineno", None)
                self._extract_script_name(item.context_expr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        attr = self._is_rpm_call(node)
        if attr is not None and attr in _KNOWN_FACTORIES:
            self.factories.add(attr)
        self.generic_visit(node)

    def _extract_script_name(self, call: ast.AST) -> None:
        # 一个模块里可能出现多个 with rpm.script(...) 块；取**第一个**遇到的字面量
        # 名字，与 self.lineno 的"首次命中"语义对齐。
        if self.script_name is not None:
            return
        assert isinstance(call, ast.Call)
        if call.args:
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self.script_name = first.value
                return
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(
                kw.value.value, str
            ):
                self.script_name = kw.value.value
                return


__all__ = ["discover", "ScriptCard"]
