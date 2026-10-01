"""命名目标 (CORE-10)。

把 ``pyproject.toml`` 的 ``[tool.redpymake.targets.<name>]`` 与项目根下
``.redpymake/targets.toml`` 做键级合并，再转发给对应工厂。解析失败一律
``TargetConfigError``，不漏出 ``TypeError`` / TOML 解析异常。
"""

from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .exceptions import TargetConfigError

_KINDS = frozenset({"local", "ssh", "adb", "serial", "wsl"})
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
_MAX_WALK = 6
_LAYER_PYPROJECT = "pyproject"
_LAYER_LOCAL = ".redpymake/targets.toml"


@dataclass(frozen=True)
class TargetInfo:
    """列出目标时的公开视图。``params`` 不含口令值。"""

    name: str
    kind: str
    source: tuple[str, ...]
    params: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "source": list(self.source),
            "params": {key: self.params[key] for key in sorted(self.params)},
        }


@dataclass(frozen=True)
class ResolvedTarget:
    """建连前的最终参数。``params`` 是工厂关键字，可能含 ``password``。"""

    name: str
    kind: str
    source: tuple[str, ...]
    params: dict[str, Any]


@dataclass(frozen=True)
class _Layer:
    source: str
    path: str
    data: dict[str, Any]


def legal_target_spec(spec: str) -> bool:
    """``board`` 与 ``@board`` 都算合法调用名。"""
    raw = spec[1:] if spec.startswith("@") else spec
    return bool(raw) and _NAME_RE.fullmatch(raw) is not None


def list_targets(root: str | os.PathLike[str] = ".") -> list[TargetInfo]:
    """合并后的目标列表，按名字排序。不建连，不读取 ``password_env``。"""
    found = _collect(root)
    infos: list[TargetInfo] = []
    for name in sorted(found):
        info, _params = _finalize(name, found[name], {}, resolve_password=False)
        infos.append(info)
    return infos


def resolve_target(
    name: str,
    *,
    root: str | os.PathLike[str] = ".",
    **overrides: Any,
) -> ResolvedTarget:
    """解析一个目标的工厂参数。不建连；``password_env`` 在这里读环境变量。"""
    key = _normalize_call_name(name)
    found = _collect(root)
    if key not in found:
        available = ", ".join(sorted(found)) if found else "(none)"
        raise TargetConfigError(
            f"unknown target {key!r}; available: {available}",
            name=key,
        )
    info, params = _finalize(key, found[key], overrides, resolve_password=True)
    return ResolvedTarget(name=info.name, kind=info.kind, source=info.source, params=params)


def target(
    name: str,
    *,
    root: str | os.PathLike[str] = ".",
    **overrides: Any,
) -> Any:
    """按名字打开会话。参数见 ``doc/core-lib-requirements.md`` § CORE-10。"""
    resolved = resolve_target(name, root=root, **overrides)
    factory = _factories()[resolved.kind]
    try:
        return factory(**resolved.params)
    except TypeError as exc:
        raise TargetConfigError(
            f"target {resolved.name!r}: {exc}",
            name=resolved.name,
            source=_source_label(resolved.source),
        ) from exc


# ---------------------------------------------------------------- 内部


def _factories() -> dict[str, Any]:
    from ._factory import adb, local, serial, ssh, wsl

    return {"local": local, "ssh": ssh, "adb": adb, "serial": serial, "wsl": wsl}


def _normalize_call_name(spec: str) -> str:
    raw = spec[1:] if spec.startswith("@") else spec
    if not raw or _NAME_RE.fullmatch(raw) is None:
        raise TargetConfigError(f"illegal target name {spec!r}", name=spec)
    return raw


def _require_config_name(name: str, *, source: str) -> None:
    if _NAME_RE.fullmatch(name) is None:
        raise TargetConfigError(
            f"illegal target name {name!r} in {source}",
            name=name,
            source=source,
        )


def _find_project_root(start: Path) -> Path | None:
    """最近的 pyproject.toml；没有则最近的 targets.toml。最多 6 个目录。"""
    candidate = start.resolve()
    toml_hit: Path | None = None
    for _ in range(_MAX_WALK):
        if (candidate / "pyproject.toml").is_file():
            return candidate
        local = candidate / ".redpymake" / "targets.toml"
        if toml_hit is None and local.is_file():
            toml_hit = candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return toml_hit


def _collect(root: str | os.PathLike[str]) -> dict[str, tuple[_Layer, ...]]:
    project = _find_project_root(Path(os.fspath(root)))
    if project is None:
        return {}
    layers: dict[str, list[_Layer]] = {}
    py_path = project / "pyproject.toml"
    if py_path.is_file():
        data = _read_toml(py_path)
        raw = _table(
            _table(
                _table(data.get("tool"), what="[tool]", source=str(py_path)).get("redpymake"),
                what="[tool.redpymake]",
                source=str(py_path),
            ).get("targets"),
            what="[tool.redpymake.targets]",
            source=str(py_path),
        )
        for name, table in raw.items():
            key = str(name)
            _require_config_name(key, source=str(py_path))
            if not isinstance(table, dict):
                raise TargetConfigError(
                    f"target {key!r} must be a table in {py_path}",
                    name=key,
                    source=str(py_path),
                )
            layers.setdefault(key, []).append(
                _Layer(_LAYER_PYPROJECT, str(py_path), dict(table))
            )
    local_path = project / ".redpymake" / "targets.toml"
    if local_path.is_file():
        data = _read_toml(local_path)
        for name, table in data.items():
            key = str(name)
            _require_config_name(key, source=str(local_path))
            if not isinstance(table, dict):
                raise TargetConfigError(
                    f"target {key!r} must be a table in {local_path}",
                    name=key,
                    source=str(local_path),
                )
            layers.setdefault(key, []).append(
                _Layer(_LAYER_LOCAL, str(local_path), dict(table))
            )
    return {name: tuple(items) for name, items in layers.items()}


def _finalize(
    name: str,
    layers: tuple[_Layer, ...],
    overrides: Mapping[str, Any],
    *,
    resolve_password: bool,
) -> tuple[TargetInfo, dict[str, Any]]:
    merged: dict[str, Any] = {}
    sources: list[str] = []
    for layer in layers:
        if "password" in layer.data:
            raise TargetConfigError(
                f"target {name!r}: literal 'password' is not allowed in {layer.source}; "
                "use password_env",
                name=name,
                source=layer.path,
            )
        merged.update(layer.data)
        if layer.source not in sources:
            sources.append(layer.source)
    merged.update(overrides)

    kind = merged.pop("kind", None)
    password_env = merged.pop("password_env", None)
    where = layers[-1].path if layers else None
    if not isinstance(kind, str) or kind not in _KINDS:
        shown = kind if isinstance(kind, str) else kind.__class__.__name__
        raise TargetConfigError(
            f"target {name!r}: kind must be one of {', '.join(sorted(_KINDS))} (got {shown!r})",
            name=name,
            source=where,
        )
    if password_env is not None and not isinstance(password_env, str):
        raise TargetConfigError(
            f"target {name!r}: password_env must be a string",
            name=name,
            source=where,
        )
    if password_env == "":
        raise TargetConfigError(
            f"target {name!r}: password_env must be a non-empty string",
            name=name,
            source=where,
        )

    if isinstance(merged.get("key_filename"), str):
        merged["key_filename"] = os.path.expanduser(merged["key_filename"])

    factory_params = dict(merged)
    if resolve_password and "password" not in factory_params and password_env:
        if password_env not in os.environ:
            raise TargetConfigError(
                f"target {name!r}: environment variable {password_env} is not set",
                name=name,
                source=where,
            )
        factory_params["password"] = os.environ[password_env]

    _check_factory_params(name, kind, factory_params, source=where)

    display = dict(merged)
    display.pop("password", None)
    if password_env is not None:
        display["password_env"] = password_env
    info = TargetInfo(name=name, kind=kind, source=tuple(sources), params=display)
    return info, factory_params


def _check_factory_params(
    name: str,
    kind: str,
    params: Mapping[str, Any],
    *,
    source: str | None,
) -> None:
    sig = inspect.signature(_factories()[kind])
    accepted: set[str] = set()
    required: set[str] = set()
    has_var_kw = False
    for pname, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_kw = True
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        accepted.add(pname)
        if param.default is inspect.Parameter.empty:
            required.add(pname)
    if not has_var_kw:
        unknown = sorted(set(params) - accepted)
        if unknown:
            raise TargetConfigError(
                f"target {name!r}: unknown keys for kind {kind!r}: {', '.join(unknown)}",
                name=name,
                source=source,
            )
    missing = sorted(required - set(params))
    if missing:
        raise TargetConfigError(
            f"target {name!r}: missing required keys for kind {kind!r}: {', '.join(missing)}",
            name=name,
            source=source,
        )


def _table(value: Any, *, what: str, source: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TargetConfigError(f"{what} must be a table in {source}", source=source)
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    lib = _toml_lib()
    try:
        with path.open("rb") as fp:
            data = lib.load(fp)
    except OSError as exc:
        raise TargetConfigError(
            f"failed to read {path}: {exc}",
            source=str(path),
        ) from exc
    except lib.TOMLDecodeError as exc:
        raise TargetConfigError(
            f"failed to parse {path}: {exc}",
            source=str(path),
        ) from exc
    if not isinstance(data, dict):
        raise TargetConfigError(f"{path} must be a table", source=str(path))
    return data


def _toml_lib() -> Any:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        import tomli as tomllib  # type: ignore
    return tomllib


def _source_label(source: tuple[str, ...]) -> str | None:
    return "+".join(source) or None


__all__ = [
    "ResolvedTarget",
    "TargetInfo",
    "legal_target_spec",
    "list_targets",
    "resolve_target",
    "target",
]
