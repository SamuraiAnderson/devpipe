"""NDJSON 实时 sink (CORE-10)。

``ScriptRun.__enter__`` 时读取环境变量 ``REDPYMAKE_LIVE_SINK``：

- ``file:///abs/path/to/live.ndjson``：追加写；父目录自动 ``makedirs``；
- 缺省或非 ``file://`` 前缀：不激活。

sink 通过 ``SessionLogs.subscribe(...)`` 挂到已 attach 的 root Session；同时
把 ``SessionLogRecord`` 类型（`user_log`、命令、传输、系统事件）以及 ``script.begin``
/ ``script.end`` 元行落成 NDJSON。

每一行在写主文件的同时，还镜像一份到 ``<主文件目录>/sessions/<slug>.ndjson``
（按 ``session_id`` 分文件；无 session 的元行进 ``__script__.ndjson``）。主文件
始终是权威的全量顺序流，分文件只写不读——删掉不影响任何功能。

写入失败（磁盘满 / 权限）只走 ``_diag_logger.exception(...)`` 不掩盖用户
异常；``close()`` 幂等。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, TextIO

from ._logs import SessionLogRecord

_diag_logger = logging.getLogger("redpymake")

_ENV_VAR = "REDPYMAKE_LIVE_SINK"

#: 分文件所在子目录名（相对主文件所在目录）
SESSIONS_DIRNAME = "sessions"
#: 无 ``session_id`` 的行归到这份文件
SCRIPT_STEM = "__script__"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_STEM = 64


def session_log_filename(session_id: str | None) -> str:
    """把 ``session_id`` 映射成分文件名。

    session_id 形如 ``wsl:default#1`` / ``ssh:user@host#2``，含 Windows 非法字符，
    必须安全化。裸替换会让 ``wsl:default#1`` 与 ``wsl_default_1`` 撞名，所以只要
    发生了替换、截断或撞上保留名，就补一个 sha1 短后缀区分。
    """
    if not session_id:
        return SCRIPT_STEM + ".ndjson"
    stem = _UNSAFE_CHARS.sub("_", session_id)
    if stem != session_id or len(stem) > _MAX_STEM or stem == SCRIPT_STEM:
        digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
        stem = stem[:_MAX_STEM] + "-" + digest
    return stem + ".ndjson"


class SessionFanout:
    """按 ``session_id`` 把行镜像到 ``sessions/`` 下的分文件。

    句柄按需打开、常驻到 ``close()``；一路会话写失败只记诊断日志，不影响主文件、
    也不影响其它会话。``mirror_line`` 收的是**已序列化好的整行**，保证分文件与
    主文件逐字符一致。
    """

    __slots__ = ("_dir", "_handles", "_lock", "_closed", "_broken")

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._handles: dict[str, TextIO] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._broken: set[str] = set()

    @property
    def directory(self) -> Path:
        return self._dir

    def mirror_line(self, session_id: str | None, line: str) -> None:
        name = session_log_filename(session_id)
        try:
            with self._lock:
                if self._closed:
                    return
                fp = self._handles.get(name)
                if fp is None:
                    if name in self._broken:
                        return
                    self._dir.mkdir(parents=True, exist_ok=True)
                    fp = open(self._dir / name, "a", encoding="utf-8", newline="\n")
                    self._handles[name] = fp
                fp.write(line + "\n")
                fp.flush()
        except Exception:  # 分文件是旁路，坏了就放弃这一路，主文件继续
            _diag_logger.exception("failed to mirror line to %s", self._dir / name)
            with self._lock:
                self._broken.add(name)
                fp = self._handles.pop(name, None)
            if fp is not None:
                try:
                    fp.close()
                except Exception:  # pragma: no cover
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = list(self._handles.values())
            self._handles.clear()
        for fp in handles:
            try:
                fp.close()
            except Exception:  # pragma: no cover
                _diag_logger.exception("failed to close session mirror file")


def parse_sink_uri(uri: str | None) -> Path | None:
    """把 sink URI 解析成本地文件 Path。

    第一版仅支持 ``file://<abs-path>``；其它 scheme 一律返回 ``None``（等价"未激活"）。
    - Windows 上 ``file:///C:/x/y`` 与 ``file://C:/x/y`` 都能识别；
    - 允许直接给纯路径（无 ``file://`` 前缀）时也接受，方便测试注入。
    """
    if not uri:
        return None
    stripped = uri.strip()
    if not stripped:
        return None
    parsed = urllib.parse.urlparse(stripped)
    if parsed.scheme == "" and stripped:
        # 允许裸路径
        return Path(stripped)
    if parsed.scheme != "file":
        return None
    # 兼容 file:///C:/x 与 file://host/x；忽略 host
    raw = urllib.parse.unquote(parsed.path)
    # Windows 情形："/C:/x/y" → "C:/x/y"
    if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw) if raw else None


def sink_path_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    """从环境变量读 sink 路径；默认读 ``os.environ``。"""
    env = env if env is not None else os.environ
    return parse_sink_uri(env.get(_ENV_VAR))


class NdjsonLiveSink:
    """NDJSON 追加写 sink，被 ``ScriptRun`` 生命周期驱动。

    - ``open()``：创建父目录、打开文件（``append`` 模式）、写入 ``script.begin`` 元行；
    - ``on_record(rec)``：把一条 ``SessionLogRecord`` 序列化写入；
    - ``close(exception=...)``：写入 ``script.end`` 元行、关闭文件；幂等。
    """

    __slots__ = (
        "_path", "_script_name", "_started_at", "_pid",
        "_fp", "_lock", "_closed", "_fanout",
    )

    def __init__(self, path: Path, script_name: str, started_at: float) -> None:
        self._path = path
        self._script_name = script_name
        self._started_at = started_at
        self._pid = os.getpid()
        self._fp: TextIO | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._fanout = SessionFanout(path.parent / SESSIONS_DIRNAME)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def sessions_dir(self) -> Path:
        return self._fanout.directory

    def open(self) -> None:
        try:
            if self._path.parent and not self._path.parent.exists():
                self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = open(self._path, "a", encoding="utf-8", newline="\n")
        except OSError:
            _diag_logger.exception("failed to open live sink %s", self._path)
            self._fp = None
            return
        self._write_line(
            {
                "event": "script.begin",
                "name": self._script_name,
                "pid": self._pid,
                "started_at": self._started_at,
                "timestamp": self._started_at,
            }
        )

    def on_record(self, record: SessionLogRecord) -> None:
        if self._closed or self._fp is None:
            return
        payload: dict[str, Any] = {
            "timestamp": record.timestamp,
            "sequence": record.sequence,
            "session_id": record.session_id,
            "event": record.event,
            "level": record.level,
            "stream": record.stream,
            "message": record.message,
            "operation_id": record.operation_id,
            "fields": _sanitize_fields(record.fields),
        }
        self._write_line(payload)

    def close(
        self,
        *,
        ended_at: float | None = None,
        exception: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fp = self._fp
            self._fp = None
        if fp is None:
            return
        end_time = ended_at if ended_at is not None else time.time()
        try:
            line = json.dumps(
                {
                    "event": "script.end",
                    "name": self._script_name,
                    "ended_at": end_time,
                    "exception": dict(exception) if exception else None,
                    "timestamp": end_time,
                },
                ensure_ascii=False,
            )
            fp.write(line + "\n")
            fp.flush()
            self._fanout.mirror_line(None, line)
        except Exception:  # pragma: no cover - 写盘失败仅记录
            _diag_logger.exception("failed to write script.end to %s", self._path)
        finally:
            self._fanout.close()
            try:
                fp.close()
            except Exception:  # pragma: no cover
                _diag_logger.exception("failed to close live sink %s", self._path)

    def _write_line(self, payload: Mapping[str, Any]) -> None:
        fp = self._fp
        if fp is None:
            return
        try:
            line = json.dumps(payload, ensure_ascii=False)
            with self._lock:
                if self._closed or self._fp is None:
                    return
                fp.write(line + "\n")
                fp.flush()
        except Exception:  # pragma: no cover - 写盘失败仅记录
            _diag_logger.exception("failed to write live sink line to %s", self._path)
            return
        # 主文件落成之后才镜像：分文件出问题不能连累主流
        self._fanout.mirror_line(payload.get("session_id"), line)


def _sanitize_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 fields 里不可序列化的对象降级为 ``repr(...)``。"""
    if not fields:
        return {}
    result: dict[str, Any] = {}
    for key, value in fields.items():
        try:
            json.dumps(value)
            result[key] = value
        except (TypeError, ValueError):
            result[key] = repr(value)
    return result


__all__ = [
    "NdjsonLiveSink",
    "SessionFanout",
    "parse_sink_uri",
    "session_log_filename",
    "sink_path_from_env",
]
