"""过时判断顶级函数 ``rpm.stale`` (CORE-05)。

设计要点：

- 无状态函数式 API：不引入 ``Rule`` / ``MakeContext`` 等对象。
- 默认策略 ``"mtime"``：比较目标与依赖的修改时间；缺依赖始终抛异常，缺目标视为
  过时。
- 策略可插拔：``strategy`` 可以是字符串、``StalePredicate`` 协议、或用户自定义
  callable。
- 每次调用向所属会话缓冲写入一条 ``stale.check`` 结构化日志。
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable, Protocol, Sequence

from ._logs import LogBuffer
from ._path import ResourcePath, ResourceStat
from .exceptions import InputNotFoundError, UnsupportedOperationError

PathSpec = "str | os.PathLike[str] | ResourcePath"


class StalePredicate(Protocol):
    """自定义过时策略协议。"""

    def __call__(
        self,
        targets: Sequence[ResourcePath],
        sources: Sequence[ResourcePath],
    ) -> bool: ...


def _as_iterable(value: Any) -> list[Any]:
    """将 ``PathSpec | Iterable[PathSpec]`` 归一化为列表。

    单个 ``str``/``os.PathLike``/``ResourcePath`` 也视为单元素列表。
    """
    if value is None:
        return []
    if isinstance(value, (str, os.PathLike, ResourcePath)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _coerce_local_path(spec: Any) -> ResourcePath:
    """将本地 ``str``/``os.PathLike`` 提升为绑定到本地会话的 ``ResourcePath``。

    对于纯字符串/``PathLike``，按需求约定基于**当前进程目录**解析，而不是默认
    local 会话首次创建时的 cwd。
    """
    if isinstance(spec, ResourcePath):
        return spec
    from ._factory import _default_local  # 延迟导入
    from ._path import resolve_against  # 局部导入避免顶部循环

    session = _default_local()
    resolved = resolve_against(session, spec, cwd=os.getcwd())
    return ResourcePath(session, os.fspath(spec), resolved)


def _pick_log_buffer(paths: Sequence[ResourcePath]) -> LogBuffer | None:
    for p in paths:
        buf = getattr(p.session, "_log_buffer", None)
        if buf is not None:
            return buf
    return None


def _mtime_predicate(
    targets: Sequence[ResourcePath],
    sources: Sequence[ResourcePath],
) -> tuple[bool, str, dict[str, Any]]:
    """默认 mtime 策略。返回 (是否过时, 原因标记, 结构化字段)。"""

    # 依赖：任一不存在 → 抛
    source_mtimes: list[float] = []
    for src in sources:
        if not src.exists():
            raise InputNotFoundError(
                f"stale dependency does not exist: {src}",
                path=src,
            )
        stat: ResourceStat = src.stat()
        source_mtimes.append(stat.mtime)

    # 目标：任一不存在 → True
    target_mtimes: list[float] = []
    missing_target: ResourcePath | None = None
    for tgt in targets:
        if not tgt.exists():
            missing_target = tgt
            break
        target_mtimes.append(tgt.stat().mtime)

    if missing_target is not None:
        return (
            True,
            "target_missing",
            {"missing_target": str(missing_target)},
        )

    if not target_mtimes:
        # 无目标可比：视为过时（例如 targets=[]）
        return True, "no_target", {}

    if not source_mtimes:
        # 无依赖时，只要目标存在就视为最新。
        return False, "up_to_date", {}

    newest_source = max(source_mtimes)
    oldest_target = min(target_mtimes)
    if newest_source > oldest_target:
        return (
            True,
            "source_newer",
            {
                "newest_source_mtime": newest_source,
                "oldest_target_mtime": oldest_target,
            },
        )
    return False, "up_to_date", {}


def stale(
    target: Any,
    depends_on: Any = (),
    *,
    strategy: str | StalePredicate = "mtime",
    name: str | None = None,
) -> bool:
    """判断 ``target`` 是否相对 ``depends_on`` 已过时（需要重做）。

    - ``target`` / ``depends_on`` 支持单个 ``PathSpec`` 或其可迭代集合。
    - ``strategy``：``"mtime"``（默认，仅时间戳）/ ``"hash"``（预留，抛
      ``UnsupportedOperationError``）/ 自定义 callable。
    - ``name``：日志中用于聚合的可读标识。
    - 依赖不存在始终抛 ``InputNotFoundError``；目标不存在返回 ``True``。
    """

    started = time.monotonic()

    target_paths = [_coerce_local_path(p) for p in _as_iterable(target)]
    source_paths = [_coerce_local_path(p) for p in _as_iterable(depends_on)]

    # 选定日志缓冲：优先第一个路径所属会话，否则回落到 stale 相关的 root logger
    log_buffer = _pick_log_buffer(target_paths) or _pick_log_buffer(source_paths)

    result: bool
    reason: str
    fields: dict[str, Any] = {}

    try:
        if isinstance(strategy, str):
            if strategy == "mtime":
                result, reason, extra = _mtime_predicate(target_paths, source_paths)
                fields.update(extra)
            elif strategy == "hash":
                raise UnsupportedOperationError(
                    "stale strategy 'hash' is reserved for a future version",
                )
            else:
                raise ValueError(f"unknown stale strategy: {strategy!r}")
        elif callable(strategy):
            result = bool(strategy(target_paths, source_paths))
            reason = "custom_true" if result else "custom_false"
        else:
            raise TypeError(
                "strategy must be a string, callable, or StalePredicate"
            )
    except InputNotFoundError as exc:
        # 依赖缺失也要落一条日志，随后再抛
        elapsed = time.monotonic() - started
        if log_buffer is not None:
            log_buffer.append(
                event="stale.check",
                level="ERROR",
                stream="system",
                message=(
                    f"stale check failed: dependency missing: {exc.path}"
                ),
                fields={
                    "name": name,
                    "strategy": strategy if isinstance(strategy, str) else "custom",
                    "result": "error",
                    "reason": "input_not_found",
                    "targets": [str(p) for p in target_paths],
                    "depends_on": [str(p) for p in source_paths],
                    "elapsed": elapsed,
                    "missing": str(exc.path),
                },
            )
        exc.name = name
        raise

    elapsed = time.monotonic() - started
    if log_buffer is not None:
        log_buffer.append(
            event="stale.check",
            level="INFO",
            stream="system",
            message=(
                f"stale check {name or '(anonymous)'}: "
                f"{'stale' if result else 'up_to_date'} ({reason})"
            ),
            fields={
                "name": name,
                "strategy": strategy if isinstance(strategy, str) else "custom",
                "result": bool(result),
                "reason": reason,
                "targets": [str(p) for p in target_paths],
                "depends_on": [str(p) for p in source_paths],
                "elapsed": elapsed,
                **fields,
            },
        )
    return result


__all__ = ["stale", "StalePredicate", "PathSpec"]
