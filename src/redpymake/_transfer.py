"""文件传输结果与 push/pull/copy 语义 (CORE-04)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ._path import ResourcePath


@dataclass(frozen=True)
class TransferResult:
    """一次 push/pull/copy 的结构化结果。

    Attributes:
        source: 源资源路径。
        target: 目标资源路径。
        transferred: 是否实际发生了字节传输（否则可能因 ``overwrite=False`` 而跳过）。
        bytes_transferred: 实际传输的字节数；未传输时为 ``0``。
        duration: 耗时（秒）。
    """

    source: "ResourcePath"
    target: "ResourcePath"
    transferred: bool
    bytes_transferred: int
    duration: float


__all__ = ["TransferResult"]
