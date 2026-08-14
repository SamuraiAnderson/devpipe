"""``FakeRemoteSession``：一个基于内存字典的"非本地"会话。

用途：在不依赖真实 SSH / ADB / 串口设备的前提下，驱动 ``Session`` 中与
"跨会话传输"相关的分支（CORE-04）：

- ``_is_local = False`` 以进入 ``_do_copy_from`` / ``_do_copy_into`` /
  ``_copy_via_local_tmp`` 路径；
- 提供最小的 ``_resource_*`` 语义（内存字典）；
- ``_execute_command`` 抛 ``UnsupportedOperationError``，仅用于传输测试；

路径风格固定为 posix，工作目录默认 ``"/"``。
"""

from __future__ import annotations

import os
import time
from typing import Mapping, Sequence

from redpymake._path import ResourceStat
from redpymake._session import Session
from redpymake.exceptions import (
    ResourceNotFoundError,
    UnsupportedOperationError,
)


class FakeRemoteSession(Session):
    """内存字典模拟的远端会话。仅供测试使用。"""

    _is_local = False

    def __init__(self, label: str = "fake-remote") -> None:
        super().__init__(
            session_kind="fake",
            session_label=label,
            default_cwd="/",
        )
        self.path_style = "posix"
        self.home_dir = "/root"
        # 简单的 in-memory 文件系统：路径 -> (bytes, mtime)
        self._files: dict[str, tuple[bytes, float]] = {}
        # 已存在的目录集合，默认包含根目录
        self._dirs: set[str] = {"/"}
        self._log_buffer.append(
            event="session_open",
            level="INFO",
            stream="system",
            message=f"fake-remote session ready ({label})",
        )

    # ------------------------------ 命令 ------------------------------

    def _execute_command(
        self,
        argv: Sequence[str],
        *,
        shell: bool,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout: float | None,
        encoding: str,
        operation_id: str,
    ) -> tuple[int, str, str]:
        raise UnsupportedOperationError(
            "fake-remote session does not execute commands"
        )

    # ------------------------------ 资源 ------------------------------

    def _resource_exists(self, path: str) -> bool:
        return path in self._files or path in self._dirs

    def _resource_is_file(self, path: str) -> bool:
        return path in self._files

    def _resource_is_dir(self, path: str) -> bool:
        return path in self._dirs

    def _resource_stat(self, path: str) -> ResourceStat:
        if path in self._files:
            data, mtime = self._files[path]
            return ResourceStat(size=len(data), mtime=mtime, is_dir=False)
        if path in self._dirs:
            return ResourceStat(size=0, mtime=0.0, is_dir=True)
        raise ResourceNotFoundError(
            f"fake-remote resource not found: {path}", path=path
        )

    def _resource_remove(self, path: str, *, missing_ok: bool = False) -> None:
        if path in self._files:
            del self._files[path]
            return
        if path in self._dirs and path != "/":
            self._dirs.discard(path)
            return
        if not missing_ok:
            raise ResourceNotFoundError(
                f"fake-remote resource not found: {path}", path=path
            )

    def _resource_mkdir(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path in self._dirs:
            if not exist_ok:
                raise FileExistsError(path)
            return
        if parents:
            acc = ""
            for part in path.strip("/").split("/"):
                acc = f"{acc}/{part}" if acc else f"/{part}"
                self._dirs.add(acc)
            return
        parent = "/".join(path.rstrip("/").split("/")[:-1]) or "/"
        if parent not in self._dirs:
            raise FileNotFoundError(parent)
        self._dirs.add(path)

    # ------------------------------ 传输 ------------------------------

    def _do_copy_into(
        self,
        local_source: str,
        remote_target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        if remote_target in self._files and not overwrite:
            return 0, False
        with open(local_source, "rb") as fp:
            data = fp.read()
        self._files[remote_target] = (data, time.time())
        return len(data), True

    def _do_copy_from(
        self,
        remote_source: str,
        local_target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        if remote_source not in self._files:
            raise ResourceNotFoundError(
                f"fake-remote source not found: {remote_source}",
                path=remote_source,
            )
        if os.path.exists(local_target) and not overwrite:
            return 0, False
        parent = os.path.dirname(local_target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data, _ = self._files[remote_source]
        with open(local_target, "wb") as fp:
            fp.write(data)
        return len(data), True

    def _do_copy_within(
        self,
        source: str,
        target: str,
        *,
        overwrite: bool,
        timeout: float | None,
    ) -> tuple[int, bool]:
        if source not in self._files:
            raise ResourceNotFoundError(
                f"fake-remote source not found: {source}", path=source
            )
        if target in self._files and not overwrite:
            return 0, False
        data, _ = self._files[source]
        self._files[target] = (data, time.time())
        return len(data), True

    # ------------------------------ 便利接口 ------------------------------

    def put_file(self, path: str, data: bytes) -> None:
        """在假 FS 中直接放一个文件，便于测试准备。"""
        self._files[path] = (data, time.time())

    def read_file(self, path: str) -> bytes:
        """在假 FS 中读取一个文件的字节，便于测试断言。"""
        return self._files[path][0]


__all__ = ["FakeRemoteSession"]
