"""按 controller.host 隔离的命令历史持久化服务。

每个 host 对应 history/ 目录下的一个 txt 文件，每行一条命令。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_HISTORY_DIR = Path(__file__).resolve().parents[2] / "history"

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')


def _safe_filename(host: str) -> str:
    """将 host 中的文件名不安全字符替换为 '_'。"""
    return _UNSAFE_CHARS.sub("_", host)


@dataclass
class CommandHistory:
    """管理单个 host 的命令历史。"""

    host: str
    max_size: int = 500
    _cache: list[str] = field(default_factory=list, repr=False, init=False)
    _loaded: bool = field(default=False, repr=False, init=False)

    def _path(self) -> Path:
        return _HISTORY_DIR / f"{_safe_filename(self.host)}.txt"

    def load(self) -> list[str]:
        """读取全部历史命令，返回列表（旧→新）。"""
        if self._loaded:
            return self._cache
        path = self._path()
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            self._cache = [ln for ln in text.splitlines() if ln]
        else:
            self._cache = []
        self._loaded = True
        return self._cache

    def add(self, cmd: str) -> None:
        """追加一条命令。跳过与末尾重复的命令，超限时截断旧记录。"""
        cmd = cmd.strip()
        if not cmd:
            return
        history = self.load()
        if history and history[-1] == cmd:
            return

        history.append(cmd)
        if len(history) > self.max_size:
            history[:] = history[-self.max_size:]

        self._flush(history)

    def _flush(self, history: list[str]) -> None:
        path = self._path()
        _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(history) + "\n", encoding="utf-8")


_instances: dict[str, CommandHistory] = {}


def get_history(host: str) -> CommandHistory:
    """获取或创建指定 host 的 CommandHistory 单例。"""
    if host not in _instances:
        _instances[host] = CommandHistory(host=host)
    return _instances[host]
