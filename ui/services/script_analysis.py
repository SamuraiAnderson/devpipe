"""AST-based static analysis for task scripts.

Parses Python scripts to extract controller instantiations (AdbCnet, Linux,
LocalHost, SerialControl) and service instantiations (TftpdServer) along with
their constructor parameters without executing the script.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

KNOWN_CONTROLLERS: dict[str, str] = {
    "AdbCnet": "Android",
    "Linux": "Linux",
    "LocalHost": "Local",
    "SerialControl": "Serial",
}

CONTROLLER_PARAM_NAMES: dict[str, list[str]] = {
    "AdbCnet": ["host", "user"],
    "Linux": ["host", "user"],
    "LocalHost": [],
    "SerialControl": ["port", "baudrate", "timeout", "mode"],
}

KNOWN_SERVICES: dict[str, str] = {
    "TftpdServer": "TFTP",
}

SERVICE_PARAM_NAMES: dict[str, list[str]] = {
    "TftpdServer": ["root_dir", "host", "port"],
}

_ALL_KNOWN: dict[str, str] = {**KNOWN_CONTROLLERS, **KNOWN_SERVICES}
_ALL_PARAM_NAMES: dict[str, list[str]] = {**CONTROLLER_PARAM_NAMES, **SERVICE_PARAM_NAMES}


@dataclass
class ControllerInfo:
    class_name: str
    platform: str
    var_name: Optional[str]
    params: dict[str, str] = field(default_factory=dict)
    kind: str = "controller"


def _resolve_arg(node: ast.expr) -> str:
    """Best-effort extraction of a human-readable value from an AST node."""
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.JoinedStr):
        return "<f-string>"
    return "<expr>"


def _collect_imports(tree: ast.Module) -> dict[str, str]:
    """Walk the *entire* AST and build a mapping {local_name -> class_name}
    for every import of a known controller or service class, including imports
    nested inside functions."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if alias.name in _ALL_KNOWN:
                    mapping[name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                if name in _ALL_KNOWN:
                    mapping[name] = name
    return mapping


def _find_instantiations(
    tree: ast.Module,
    import_map: dict[str, str],
) -> list[ControllerInfo]:
    """Walk the AST for Call nodes that instantiate known controllers or services."""
    results: list[ControllerInfo] = []
    seen_keys: set[tuple[str, str | None]] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_name: str | None = None
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            call_name = node.func.attr

        if call_name is None or call_name not in import_map:
            continue

        class_name = import_map[call_name]
        platform = _ALL_KNOWN[class_name]
        param_names = _ALL_PARAM_NAMES.get(class_name, [])
        kind = "service" if class_name in KNOWN_SERVICES else "controller"

        params: dict[str, str] = {}
        for idx, arg in enumerate(node.args):
            key = param_names[idx] if idx < len(param_names) else f"arg{idx}"
            params[key] = _resolve_arg(arg)
        for kw in node.keywords:
            if kw.arg is not None:
                params[kw.arg] = _resolve_arg(kw.value)

        var_name = _infer_var_name(node)

        key = (class_name, var_name)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        results.append(ControllerInfo(
            class_name=class_name,
            platform=platform,
            var_name=var_name,
            params=params,
            kind=kind,
        ))

    return results


def _infer_var_name(call_node: ast.Call) -> Optional[str]:
    """Try to figure out the variable name the call result is assigned to.

    Works for simple ``x = Cls(...)`` patterns when the parent is available
    via line-number heuristics (ast.walk does not provide parent links).
    This is a best-effort helper; returns None when the pattern doesn't match.
    """
    return None


class _VarNameVisitor(ast.NodeVisitor):
    """Two-pass visitor: first pass collects assignment targets by line,
    second pass is used by analyze_script to annotate var_name."""

    def __init__(self) -> None:
        self.assign_targets: dict[int, str] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Call):
                self.assign_targets[node.value.lineno] = node.targets[0].id
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value and isinstance(node.value, ast.Call):
            self.assign_targets[node.value.lineno] = node.target.id
        self.generic_visit(node)


def analyze_script(script_id: str) -> list[ControllerInfo]:
    """Analyze a script file and return detected controller/service instantiations.

    *script_id* is relative to PROJECT_ROOT (e.g. ``example/main.py``).
    Each returned ``ControllerInfo`` carries a ``kind`` field: ``"controller"``
    for platform controllers, ``"service"`` for infrastructure services.
    """
    script_path = PROJECT_ROOT / script_id
    if not script_path.exists():
        log.warning("脚本不存在: %s", script_id)
        return []

    try:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError as exc:
        log.error("脚本语法错误: %s — %s", script_id, exc)
        return []

    import_map = _collect_imports(tree)
    if not import_map:
        return []

    var_visitor = _VarNameVisitor()
    var_visitor.visit(tree)

    results = _find_instantiations(tree, import_map)

    for info in results:
        if info.var_name is None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name and import_map.get(func_name) == info.class_name:
                    if node.lineno in var_visitor.assign_targets:
                        info.var_name = var_visitor.assign_targets[node.lineno]
                        break

    return results
