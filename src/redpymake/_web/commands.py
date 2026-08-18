"""Web UI 手动命令执行模块 (CORE-11 扩展)。

提供 CommandExecutor 类，处理从 Web UI 发起的手动命令执行：
- 通过 session_id 获取 Workspace 中的活跃 session
- 调用 session.run() 执行命令
- 通过 Workspace 事件系统流式推送输出
- 管理命令历史记录的持久化

依赖 Workspace 的 _emit_event 机制广播 cmd.output/cmd.finished/cmd.error 事件。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .._workspace import Workspace
    from .._session import Session


_diag_logger = __import__("logging").getLogger("redpymake")


def _prepare_commandbar_line(session: "Session", command: str) -> str:
    """把 CommandBar 的一行变成 ``session.run`` 的文本。

    串口且构造 ``newline`` 为空时补 ``\\r``（Enter = 提交一行）。已带
    ``\\r`` / ``\\n``、或会话已设非空 newline 的不补，避免与 ``run()`` 叠加。
    """
    root = session.root
    if getattr(root, "kind", "") != "serial":
        return command
    configured = getattr(root, "_newline", "") or ""
    if configured:
        return command
    if command.endswith(("\r", "\n")):
        return command
    return command + "\r"

# 历史记录文件名
_HISTORY_FILENAME = "history.json"
# 每个 session 最大历史条数
_MAX_HISTORY_PER_SESSION = 100
# 历史写入防抖延迟（秒）
_HISTORY_WRITE_DEBOUNCE = 5.0


def _read_command_history(log_dir: Path) -> dict:
    """读取命令历史，返回空结构如果文件不存在。

    返回格式：
    {
        "version": 1,
        "log_id": "...",
        "sessions": {
            "session_id": [
                {"command": "...", "timestamp": ..., "exit_code": ..., "duration": ...}
            ]
        }
    }
    """
    path = log_dir / _HISTORY_FILENAME
    if not path.exists():
        return {"version": 1, "sessions": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _diag_logger.warning(f"Failed to read command history: {exc}")
        return {"version": 1, "sessions": {}}


def _write_command_history(log_dir: Path, history: dict) -> None:
    """原子写入命令历史。"""
    path = log_dir / _HISTORY_FILENAME
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        _diag_logger.warning(f"Failed to write command history: {exc}")
        if tmp.exists():
            tmp.unlink()


def _add_to_history(
    history: dict,
    session_id: str,
    command: str,
    exit_code: int,
    duration: float,
) -> None:
    """添加一条命令到历史， enforcing FIFO 上限。"""
    if "sessions" not in history:
        history["sessions"] = {}
    if session_id not in history["sessions"]:
        history["sessions"][session_id] = []

    session_history = history["sessions"][session_id]
    session_history.append({
        "command": command,
        "timestamp": time.time(),
        "exit_code": exit_code,
        "duration": duration,
    })

    # FIFO 修剪
    if len(session_history) > _MAX_HISTORY_PER_SESSION:
        history["sessions"][session_id] = session_history[-_MAX_HISTORY_PER_SESSION:]


class CommandExecutor:
    """处理从 Web UI 发起的手动命令执行。

    与脚本内 `session.run()` 的区别：
    - 命令通过 API 端点发起，不依附于任何 ScriptRun
    - 输出通过 WebSocket 事件 `cmd.output` 流式推送
    - 历史记录独立保存到 history.json
    - 串口且 `newline` 为空时自动补 `\\r`（见 ``_prepare_commandbar_line``）
    """

    def __init__(self, workspace: "Workspace") -> None:
        self._workspace = workspace
        self._active_commands: dict[str, threading.Thread] = {}
        self._history_pending: dict[str, Any] | None = None
        self._history_timer: threading.Timer | None = None
        self._history_lock = threading.Lock()

    def get_session(self, session_id: str) -> "Session | None":
        """从 Workspace 获取 session。

        Returns:
            Session 实例，如果不存在或已关闭则返回 None
        """
        session = self._workspace.get_session_by_id(session_id)
        if session is None or session.closed:
            return None
        return session

    def execute(
        self,
        session_id: str,
        command: str,
        shell: bool = False,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> str:
        """执行命令并流式推送输出。

        Args:
            session_id: 目标 session 的 ID
            command: 要执行的命令
            shell: 是否使用 shell 模式
            cwd: 工作目录
            timeout: 超时时间（秒）

        Returns:
            command_id: 用于跟踪此次执行的 ID

        Raises:
            ValueError: session 不存在或已关闭
        """
        from fastapi import HTTPException

        session = self.get_session(session_id)
        if session is None:
            raise HTTPException(404, f"Session not found or closed: {session_id}")
        cmd_id = f"cmd-{secrets.token_hex(8)}"
        start_time = time.monotonic()

        def _execute_and_stream() -> None:
            try:
                # 记录开始
                cursor_before = session.logs.buffer.cursor()

                # 串口 CommandBar：Enter = 提交一行；历史仍用用户原文。
                wired = _prepare_commandbar_line(session, command)
                result = session.run(
                    wired,
                    shell=shell,
                    cwd=cwd,
                    timeout=timeout,
                    check=False,
                )

                # 收集并推送输出。带上原 event 类型，前端 LiveBody 才能对
                # command_start / command_end 走各自的显示规则（例如成功的
                # command_end 隐藏），否则一律被视作 command_output。
                new_records = session.logs.buffer.records(since=cursor_before)
                for rec in new_records:
                    if rec.event == "command_output":
                        self._emit_output(
                            cmd_id, session_id, rec.stream, rec.message,
                            event="command_output", level=rec.level,
                        )
                    elif rec.event == "command_start":
                        self._emit_output(
                            cmd_id, session_id, "system", rec.message,
                            event="command_start", level=rec.level,
                        )
                    elif rec.event == "command_end":
                        self._emit_output(
                            cmd_id, session_id, "system", rec.message,
                            event="command_end", level=rec.level,
                        )

                duration = time.monotonic() - start_time
                self._emit_finished(cmd_id, session_id, result.returncode, duration)

                # 保存到历史
                self._schedule_history_save(
                    session_id, command, result.returncode, duration
                )

            except Exception as exc:
                duration = time.monotonic() - start_time
                self._emit_error(cmd_id, session_id, str(exc))
                _diag_logger.exception(f"Command execution failed: {command}")
            finally:
                self._active_commands.pop(cmd_id, None)

        thread = threading.Thread(target=_execute_and_stream, daemon=True)
        self._active_commands[cmd_id] = thread
        thread.start()

        return cmd_id

    def _emit_output(
        self,
        command_id: str,
        session_id: str,
        stream: str,
        data: str,
        *,
        event: str = "command_output",
        level: str = "INFO",
    ) -> None:
        """推送命令输出事件。

        ``event`` 保留原 SessionLogRecord 事件类型（command_start/output/end），
        ``level`` 让前端能判失败（command_end 的 level 为 WARNING/ERROR 才显示）。
        """
        self._workspace._emit_event({
            "type": "cmd.output",
            "command_id": command_id,
            "session_id": session_id,
            "stream": stream,
            "data": data,
            "event": event,
            "level": level,
        })

    def _emit_finished(
        self, command_id: str, session_id: str, exit_code: int, duration: float
    ) -> None:
        """推送命令完成事件。"""
        self._workspace._emit_event({
            "type": "cmd.finished",
            "command_id": command_id,
            "session_id": session_id,
            "exit_code": exit_code,
            "duration": duration,
        })

    def _emit_error(self, command_id: str, session_id: str, error: str) -> None:
        """推送命令错误事件。"""
        self._workspace._emit_event({
            "type": "cmd.error",
            "command_id": command_id,
            "session_id": session_id,
            "error": error,
        })

    def _schedule_history_save(
        self, session_id: str, command: str, exit_code: int, duration: float
    ) -> None:
        """延迟保存历史记录（防抖）。"""
        with self._history_lock:
            if self._history_pending is None:
                self._history_pending = {"version": 1, "sessions": {}}

            _add_to_history(
                self._history_pending, session_id, command, exit_code, duration
            )

            # 重置定时器
            if self._history_timer is not None:
                self._history_timer.cancel()

            self._history_timer = threading.Timer(
                _HISTORY_WRITE_DEBOUNCE, self._flush_history
            )
            self._history_timer.start()

    def _flush_history(self) -> None:
        """将待写入的历史刷新到磁盘。"""
        with self._history_lock:
            if self._history_pending is None:
                return

            pending = self._history_pending
            self._history_pending = None
            self._history_timer = None

        # 获取当前活跃日志目录
        active_log = self._workspace.active_log
        if active_log is None:
            return

        try:
            # 合并现有历史
            existing = _read_command_history(active_log.root)
            for sid, records in pending["sessions"].items():
                if sid not in existing["sessions"]:
                    existing["sessions"][sid] = []
                existing["sessions"][sid].extend(records)
                # 修剪
                if len(existing["sessions"][sid]) > _MAX_HISTORY_PER_SESSION:
                    existing["sessions"][sid] = existing["sessions"][sid][
                        -_MAX_HISTORY_PER_SESSION:
                    ]

            _write_command_history(active_log.root, existing)
        except Exception as exc:
            _diag_logger.warning(f"Failed to flush command history: {exc}")

    def get_history(self, session_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取命令历史。

        Args:
            session_id: 可选，过滤特定 session
            limit: 最大返回条数

        Returns:
            命令历史列表，按时间倒序
        """
        active_log = self._workspace.active_log
        if active_log is None:
            return []

        try:
            history = _read_command_history(active_log.root)
            all_records: list[dict] = []

            if session_id:
                session_records = history["sessions"].get(session_id, [])
                for rec in session_records:
                    all_records.append({**rec, "session_id": session_id})
            else:
                for sid, records in history["sessions"].items():
                    for rec in records:
                        all_records.append({**rec, "session_id": sid})

            # 按时间倒序
            all_records.sort(key=lambda x: x["timestamp"], reverse=True)
            return all_records[:limit]
        except Exception as exc:
            _diag_logger.warning(f"Failed to get command history: {exc}")
            return []

    def clear_history(self, session_id: str | None = None) -> bool:
        """清空命令历史。

        Args:
            session_id: 可选，清空特定 session；为空则清空全部

        Returns:
            是否成功
        """
        active_log = self._workspace.active_log
        if active_log is None:
            return False

        try:
            history = _read_command_history(active_log.root)

            if session_id:
                history["sessions"].pop(session_id, None)
            else:
                history["sessions"] = {}

            _write_command_history(active_log.root, history)
            return True
        except Exception as exc:
            _diag_logger.warning(f"Failed to clear command history: {exc}")
            return False
