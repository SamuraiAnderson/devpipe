import logging
import threading
from collections import deque
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Optional

ALL = 'All'
SCRIPT = 'Script'

# `ScriptService` 专用 logger 名，生命周期日志进脚本 Tab；其余 `ui.*` 仅走 CLI、不进缓冲
_SCRIPT_SERVICE_LOGGER = 'ui.services.script_service'


def _is_script_service_logger(name: str) -> bool:
    return name == _SCRIPT_SERVICE_LOGGER or name.startswith(f'{_SCRIPT_SERVICE_LOGGER}.')


class LogRecord:
    __slots__ = ('timestamp', 'level', 'message', 'source')

    def __init__(self, timestamp: str, level: str, message: str, source: str = SCRIPT):
        self.timestamp = timestamp
        self.level = level
        self.message = message
        self.source = source

    def formatted(self) -> str:
        return f"{self.timestamp} [{self.level}] {self.message}"

    def formatted_all(self) -> str:
        return f"{self.timestamp} [{self.source}] [{self.level}] {self.message}"


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _PROJECT_ROOT / 'logs'


class LogBuffer:
    """Thread-safe ring buffer that stores LogRecords per source."""

    def __init__(self, maxlen: int = 1000):
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._buffers: dict[str, deque[LogRecord]] = {}

    def _ensure(self, source: str) -> deque[LogRecord]:
        buf = self._buffers.get(source)
        if buf is None:
            buf = deque(maxlen=self._maxlen)
            self._buffers[source] = buf
        return buf

    def push(self, source: str, record: LogRecord):
        with self._lock:
            self._ensure(ALL).append(record)
            if source != ALL:
                self._ensure(source).append(record)

    def get_records(self, source: str) -> list[LogRecord]:
        with self._lock:
            buf = self._buffers.get(source)
            if buf is None:
                return []
            return list(buf)

    def clear(self, source: str):
        with self._lock:
            buf = self._buffers.get(source)
            if buf is not None:
                buf.clear()

    def clear_all(self):
        with self._lock:
            for buf in self._buffers.values():
                buf.clear()


class LogFileWriter:
    """脚本执行会话内按 source 写入 `logs/`；无会话时不落盘。"""

    def __init__(self, log_dir: Path = _LOG_DIR):
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, TextIOWrapper] = {}
        self._session_active = False
        self._lock = threading.Lock()

    def _close_all_files_unlocked(self):
        for f in self._files.values():
            if f is not None and not f.closed:
                f.close()
        self._files.clear()

    def _open_new(self, source: str):
        """在已持有 ``_lock`` 时调用。"""
        old = self._files.get(source)
        if old is not None and not old.closed:
            old.close()
        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        safe = source.replace('/', '_').replace('\\', '_')
        path = self._log_dir / f'{safe}_{ts}.log'
        self._files[source] = open(path, 'a', encoding='utf-8')

    def begin_script_log_session(self, script_id: str):
        """打开或沿用磁盘日志会话。

        首次调用建 All、Script 及 AST 中各控制器的 source 文件；
        后续调用沿用已有句柄，仅为新出现的 source 追加打开。
        """
        from ui.services.script_analysis import analyze_script

        with self._lock:
            if not self._session_active:
                self._session_active = True
                self._open_new(ALL)
                self._open_new(SCRIPT)
            sources_needed = [
                info.log_source for info in analyze_script(script_id)
                if info.kind == 'controller'
            ]
            for src in sources_needed:
                if src not in self._files or self._files[src].closed:
                    self._open_new(src)

    def close_session(self):
        """关闭会话并释放所有文件句柄（由清空日志按钮触发）。"""
        with self._lock:
            self._session_active = False
            self._close_all_files_unlocked()

    def write(self, source: str, record: 'LogRecord'):
        with self._lock:
            if not self._session_active:
                return
            if source not in self._files or self._files[source].closed:
                self._open_new(source)
            f = self._files[source]
            if not f.closed:
                f.write(record.formatted() + '\n')
                f.flush()

    def rotate(self, source: str):
        with self._lock:
            if not self._session_active:
                return
            self._open_new(source)

    def rotate_all(self):
        with self._lock:
            if not self._session_active:
                return
            for source in list(self._files):
                self._open_new(source)


_file_writer: Optional[LogFileWriter] = None
_log_buffer: Optional[LogBuffer] = None


def get_file_writer() -> LogFileWriter:
    global _file_writer
    if _file_writer is None:
        _file_writer = LogFileWriter()
    return _file_writer


def get_buffer() -> LogBuffer:
    global _log_buffer
    if _log_buffer is None:
        _log_buffer = LogBuffer()
    return _log_buffer


def begin_script_log_session(script_id: str):
    """由 `ScriptService` 在启动脚本线程前调用。"""
    get_file_writer().begin_script_log_session(script_id)


def close_log_session():
    """关闭磁盘日志会话（由清空日志按钮触发）。"""
    get_file_writer().close_session()


class UILogHandler(logging.Handler):
    """将日志写入环形缓冲与 `logs/`（仅脚本磁盘会话内）；`ui.*`（除 script_service）早退。"""

    _IGNORED_LOGGERS = frozenset((
        'streamlit', 'tornado', 'watchfiles', 'httpx',
        'asyncio', 'urllib3', 'fsevents',
    ))

    def __init__(self):
        super().__init__()
        self._emitting = False
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        if self._emitting:
            return
        root_name = record.name.split('.')[0]
        if root_name in self._IGNORED_LOGGERS:
            return
        if record.name.startswith('ui.') and not _is_script_service_logger(record.name):
            return
        try:
            self._emitting = True
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = self.format(record)
            source = self._resolve_source(record)
            lr = LogRecord(ts, record.levelname, msg, source)

            get_file_writer().write(ALL, lr)
            get_file_writer().write(source, lr)
            get_buffer().push(source, lr)
        except Exception:
            self.handleError(record)
        finally:
            self._emitting = False

    @staticmethod
    def _resolve_source(record: logging.LogRecord) -> str:
        if record.name.startswith('client.'):
            parts = record.name.split('.', 2)
            if len(parts) >= 3:
                return f"{parts[1]}.{parts[2]}"
            return parts[1] if len(parts) >= 2 else SCRIPT
        return SCRIPT


_handler: Optional[UILogHandler] = None


def get_handler() -> UILogHandler:
    global _handler
    if _handler is None:
        _handler = UILogHandler()
    return _handler


def install(logger_name: Optional[str] = None, level: int = logging.DEBUG):
    """Attach the UI handler to the root (or named) logger."""
    handler = get_handler()
    handler.setLevel(level)
    logger = logging.getLogger(logger_name)
    if handler not in logger.handlers:
        logger.addHandler(handler)
