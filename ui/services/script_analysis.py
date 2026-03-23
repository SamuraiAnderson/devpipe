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

    @property
    def key(self) -> str:
        """用于 session_state 和去重的稳定标识，如 ``'Linux:my_var'`` 或 ``'Linux'``。"""
        return f"{self.platform}:{self.var_name}" if self.var_name else self.platform

    @property
    def log_source(self) -> str:
        """预测运行时日志路由的 source key，格式 ``'{platform}.{host}'``。"""
        if self.class_name == "LocalHost":
            return f"{self.platform}.localhost"
        raw = self.params.get("host") or self.params.get("port", "")
        host = raw.strip("'\"")
        if host and host not in ("<f-string>", "<expr>"):
            return f"{self.platform}.{host}"
        return self.platform


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


class _InstantiationVisitor(ast.NodeVisitor):
    """单次遍历 AST，在赋值 / with 上下文中自然获取 var_name 并提取控制器实例化信息。"""

    def __init__(self, import_map: dict[str, str]) -> None:
        self.import_map = import_map
        self.results: list[ControllerInfo] = []
        self._seen_keys: set[str] = set()

    def _try_extract(self, call: ast.Call, var_name: str | None) -> None:
        """若 *call* 是已知控制器/服务的实例化，提取参数并记录（按 key 去重）。"""
        call_name: str | None = None
        if isinstance(call.func, ast.Name):
            call_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            call_name = call.func.attr

        if not call_name or call_name not in self.import_map:
            return

        class_name = self.import_map[call_name]
        platform = _ALL_KNOWN[class_name]
        kind = "service" if class_name in KNOWN_SERVICES else "controller"
        param_names = _ALL_PARAM_NAMES.get(class_name, [])

        params: dict[str, str] = {}
        for idx, arg in enumerate(call.args):
            k = param_names[idx] if idx < len(param_names) else f"arg{idx}"
            params[k] = _resolve_arg(arg)
        for kw in call.keywords:
            if kw.arg is not None:
                params[kw.arg] = _resolve_arg(kw.value)

        info = ControllerInfo(
            class_name=class_name,
            platform=platform,
            var_name=var_name,
            params=params,
            kind=kind,
        )

        if info.key in self._seen_keys:
            return
        self._seen_keys.add(info.key)
        self.results.append(info)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            var_name = (
                node.targets[0].id
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                else None
            )
            self._try_extract(node.value, var_name)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value and isinstance(node.value, ast.Call):
            var_name = node.target.id if isinstance(node.target, ast.Name) else None
            self._try_extract(node.value, var_name)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                var_name = (
                    item.optional_vars.id
                    if isinstance(item.optional_vars, ast.Name)
                    else None
                )
                self._try_extract(item.context_expr, var_name)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Call):
            self._try_extract(node.value, None)
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

    visitor = _InstantiationVisitor(import_map)
    visitor.visit(tree)
    return visitor.results
