"""跨会话资源路径 ``ResourcePath`` (CORE-03)。

设计要点（对齐需求）：

- 路径与所属会话绑定；``at()`` 视图的相对路径基于视图工作目录解析，创建后不受
  其他工作目录变化影响。
- 提供类似 ``pathlib`` 的最小接口：``exists``/``is_file``/``is_dir``/``stat``/
  ``remove``/``mkdir`` 以及 ``name``、``parent`` 属性。
- 会话必须实现下述抽象 IO：``_resource_exists``/``_resource_is_file``/
  ``_resource_is_dir``/``_resource_stat``/``_resource_remove``/
  ``_resource_mkdir``。远程平台若不支持，则抛 ``UnsupportedOperationError``。
- 不使用不可见的长期缓存；``refresh()`` 只重置本对象的一次性缓存标志。
"""

from __future__ import annotations

import os
import posixpath
import ntpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 类型标注
    from ._session import Session


@dataclass(frozen=True)
class ResourceStat:
    """跨平台资源的最小 stat 集合。"""

    size: int
    mtime: float
    is_dir: bool


class ResourcePath:
    """绑定到某个会话的资源路径。

    构造后不可变；``at()`` 视图变化不影响已存在的 ``ResourcePath``。
    """

    __slots__ = ("_session", "_raw", "_resolved")

    def __init__(self, session: "Session", raw: str, resolved: str) -> None:
        self._session = session
        self._raw = raw
        self._resolved = resolved

    # ---------- 属性 ----------

    @property
    def session(self) -> "Session":
        return self._session

    @property
    def path(self) -> str:
        """已解析（结合 ``at()`` 工作目录）的路径字符串。"""
        return self._resolved

    @property
    def raw(self) -> str:
        """构造时用户传入的原始字符串。"""
        return self._raw

    @property
    def name(self) -> str:
        return _flavor(self._session).basename(self._resolved) or self._resolved

    @property
    def parent(self) -> "ResourcePath":
        flavor = _flavor(self._session)
        parent_str = flavor.dirname(self._resolved) or self._resolved
        return ResourcePath(self._session, parent_str, parent_str)

    # ---------- IO ----------

    def exists(self) -> bool:
        return self._session._resource_exists(self._resolved)

    def is_file(self) -> bool:
        return self._session._resource_is_file(self._resolved)

    def is_dir(self) -> bool:
        return self._session._resource_is_dir(self._resolved)

    def stat(self) -> ResourceStat:
        return self._session._resource_stat(self._resolved)

    def remove(self, *, missing_ok: bool = False) -> None:
        self._session._resource_remove(self._resolved, missing_ok=missing_ok)

    def mkdir(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        self._session._resource_mkdir(
            self._resolved, parents=parents, exist_ok=exist_ok
        )

    def refresh(self) -> None:
        """预留：清空本对象内的一次性缓存。当前实现无缓存，此方法为无操作。"""
        return None

    # ---------- 表达 ----------

    def __fspath__(self) -> str:
        # 只有本地会话可安全地暴露为 os.PathLike；远端路径此方法虽存在，但一般
        # 不应被 ``open()`` / ``os.stat()`` 等直接使用。为了在与 pathlib 类似
        # 的 API 中获得可移植性，仍返回内部字符串。
        return self._resolved

    def __repr__(self) -> str:
        return f"ResourcePath({self._resolved!r}, session={self._session!r})"

    def __str__(self) -> str:
        return self._resolved

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResourcePath):
            return NotImplemented
        return self._session is other._session and self._resolved == other._resolved

    def __hash__(self) -> int:
        return hash((id(self._session), self._resolved))


# --------------------------- flavor / 解析 ---------------------------


class _PathFlavor:
    """封装 posix / windows 的路径运算差异。"""

    def __init__(self, module: Any) -> None:
        self._mod = module

    def isabs(self, p: str) -> bool:
        return self._mod.isabs(p)

    def join(self, *parts: str) -> str:
        return self._mod.join(*parts)

    def normpath(self, p: str) -> str:
        return self._mod.normpath(p)

    def basename(self, p: str) -> str:
        return self._mod.basename(p)

    def dirname(self, p: str) -> str:
        return self._mod.dirname(p)


def _flavor(session: "Session") -> _PathFlavor:
    if getattr(session, "path_style", "posix") == "windows":
        return _PathFlavor(ntpath)
    return _PathFlavor(posixpath)


def resolve_against(
    session: "Session",
    raw: str | os.PathLike[str],
    *,
    cwd: str | None,
) -> str:
    """按会话的路径风格结合工作目录解析 ``raw`` 为绝对/规范路径。

    - ``~`` 前缀由会话侧提供 ``home_dir`` 属性时展开；否则原样保留。
    - 绝对路径不再拼接 ``cwd``。
    - 结果调用 ``normpath`` 消除 ``.``/``..``。
    """
    raw_str = os.fspath(raw)
    flavor = _flavor(session)

    if raw_str.startswith("~"):
        home = getattr(session, "home_dir", None)
        if home:
            tail = raw_str[1:]
            if tail.startswith("/") or tail.startswith(os.sep) or tail == "":
                raw_str = flavor.join(home, tail.lstrip("/\\")) if tail else home
            else:
                # 保留 ~user 之类不做扩展；由平台自行处理
                pass

    if flavor.isabs(raw_str):
        return flavor.normpath(raw_str)

    base = cwd or getattr(session, "default_cwd", None) or ""
    if not base:
        return flavor.normpath(raw_str)
    return flavor.normpath(flavor.join(base, raw_str))


__all__ = ["ResourcePath", "ResourceStat", "resolve_against"]
