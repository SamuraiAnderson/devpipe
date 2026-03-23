from __future__ import annotations

import builtins
import ctypes
import importlib
import logging
import os
import queue
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from ui.services.log_service import begin_script_log_session

log = logging.getLogger(__name__)

_original_input = builtins.input


class ScriptInputProxy:
    """通过 builtins.input 猴子补丁拦截脚本工作线程的 input() 调用。"""

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._waiting = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def install(self, thread: threading.Thread):
        self._thread = thread

        def patched_input(prompt=''):
            if threading.current_thread() is not self._thread:
                return _original_input(prompt)
            if prompt:
                log.info(prompt)
            self._waiting.set()
            result = self._queue.get()
            self._waiting.clear()
            return result

        builtins.input = patched_input

    def uninstall(self):
        builtins.input = _original_input

    @property
    def waiting_for_input(self) -> bool:
        return self._waiting.is_set()

    def provide_input(self, text: str):
        self._queue.put(text)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class RunState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"


@dataclass
class ScriptNode:
    """Represents a file or folder in the script tree."""
    id: str
    label: str
    path: Path
    is_dir: bool
    children: list[ScriptNode]


class ScriptService:
    def __init__(self, script_dir: str | Path | None = None):
        self._script_dir = Path(script_dir) if script_dir else PROJECT_ROOT / "example"
        self._state = RunState.IDLE
        self._running_id: Optional[str] = None
        self._current_thread: Optional[threading.Thread] = None
        self._on_state_change: Optional[Callable[[RunState, Optional[str]], None]] = None
        self._input_proxy: Optional[ScriptInputProxy] = None

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def running_id(self) -> Optional[str]:
        return self._running_id

    @state.setter
    def state(self, value: RunState):
        self._state = value
        if value != RunState.RUNNING:
            self._running_id = None
        if self._on_state_change:
            self._on_state_change(value, self._running_id)

    def on_state_change(self, fn: Callable[[RunState, Optional[str]], None]):
        self._on_state_change = fn

    def scan(self) -> list[dict]:
        """Scan the script directory and return a nested tree of dicts."""
        return self._build_tree(self._script_dir)

    def _build_tree(self, directory: Path) -> list[dict]:
        nodes: list[dict] = []
        if not directory.exists():
            return nodes
        for entry in sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
            if entry.name.startswith(('.', '__')):
                continue
            if entry.is_dir():
                children = self._build_tree(entry)
                if children:
                    nodes.append({
                        'id': str(entry.relative_to(PROJECT_ROOT)),
                        'label': entry.name,
                        'children': children,
                    })
            elif entry.suffix == '.py':
                nodes.append({
                    'id': str(entry.relative_to(PROJECT_ROOT)),
                    'label': entry.name,
                })
        return nodes

    def run_script(self, script_id: str):
        """Execute a script in a background thread."""
        if self._state == RunState.RUNNING:
            log.warning("已有脚本在运行中")
            return

        script_path = PROJECT_ROOT / script_id
        if not script_path.exists():
            log.error("脚本不存在: %s", script_id)
            return

        self._running_id = script_id
        self.state = RunState.RUNNING
        log.info("开始执行: %s", script_id)
        begin_script_log_session(script_id)

        def _worker():
            input_proxy = ScriptInputProxy()
            self._input_proxy = input_proxy
            input_proxy.install(threading.current_thread())
            try:
                module_path = script_id.replace(os.sep, '.').replace('/', '.').removesuffix('.py')
                mod = importlib.import_module(module_path)
                if hasattr(mod, 'make_test'):
                    mod.make_test()
                elif hasattr(mod, 'main'):
                    mod.main()
                else:
                    exec(compile(script_path.read_text(encoding='utf-8'), str(script_path), 'exec'))
                log.info("执行完成: %s", script_id)
                self.state = RunState.FINISHED
            except Exception as exc:
                log.error("执行失败: %s — %s", script_id, exc)
                self.state = RunState.ERROR
            finally:
                input_proxy.uninstall()
                self._input_proxy = None

        self._current_thread = threading.Thread(target=_worker, daemon=True)
        self._current_thread.start()

    def stop_script(self):
        """Request the running script thread to stop."""
        if self._state != RunState.RUNNING or self._current_thread is None:
            return
        log.warning("正在停止脚本...")
        try:
            tid = self._current_thread.ident
            if tid is not None:
                res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt)
                )
                if res == 0:
                    log.error("停止失败: 线程不存在")
                elif res > 1:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
                    log.error("停止失败: 异常设置错误")
                else:
                    log.info("已发送停止信号")
        except Exception as exc:
            log.error("停止异常: %s", exc)
        self.state = RunState.IDLE

    def is_running(self) -> bool:
        return self._state == RunState.RUNNING

    @property
    def waiting_for_input(self) -> bool:
        proxy = self._input_proxy
        return proxy is not None and proxy.waiting_for_input

    def provide_input(self, text: str):
        proxy = self._input_proxy
        if proxy is not None:
            proxy.provide_input(text)
