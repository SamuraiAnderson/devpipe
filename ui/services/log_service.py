import logging
import os
import threading
from collections import deque
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

_HOST_TO_SOURCE: dict[str, str] = {}


def _init_host_map():
    """Build a mapping from host string to platform label using .env values."""
    _HOST_TO_SOURCE['localhost'] = 'Local'
    linux_host = os.getenv('LINUX_HOST', '')
    android_host = os.getenv('ANDROID_HOST', '')
    if linux_host:
        _HOST_TO_SOURCE[linux_host] = 'Linux'
    if android_host:
        _HOST_TO_SOURCE[android_host] = 'Android'


_init_host_map()

ALL = 'All'


class LogRecord:
    __slots__ = ('timestamp', 'level', 'message', 'source')

    def __init__(self, timestamp: str, level: str, message: str, source: str = 'General'):
        self.timestamp = timestamp
        self.level = level
        self.message = message
        self.source = source

    def formatted(self) -> str:
        return f"{self.timestamp} [{self.level}] {self.message}"


SOURCES = [ALL, 'Local', 'Linux', 'Android']

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _PROJECT_ROOT / 'logs'


class LogBuffer:
    """Thread-safe ring buffer that stores LogRecords per source."""

    def __init__(self, maxlen: int = 1000):
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._buffers: dict[str, deque[LogRecord]] = {
            source: deque(maxlen=maxlen) for source in SOURCES
        }

    def push(self, source: str, record: LogRecord):
        with self._lock:
            buf = self._buffers.get(ALL)
            if buf is not None:
                buf.append(record)
            buf = self._buffers.get(source)
            if buf is not None:
                buf.append(record)

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
    """Manages per-source log files under a central directory."""

    def __init__(self, log_dir: Path = _LOG_DIR):
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, TextIOWrapper] = {}
        for source in SOURCES:
            self._open_new(source)

    def _open_new(self, source: str):
        old = self._files.get(source)
        if old is not None and not old.closed:
            old.close()
        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        path = self._log_dir / f'{source}_{ts}.log'
        self._files[source] = open(path, 'a', encoding='utf-8')

    def write(self, source: str, record: 'LogRecord'):
        f = self._files.get(source)
        if f is not None and not f.closed:
            f.write(record.formatted() + '\n')
            f.flush()

    def rotate(self, source: str):
        if source in self._files:
            self._open_new(source)

    def rotate_all(self):
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


class UILogHandler(logging.Handler):
    """Routes Python logging records to a ring buffer and log files."""

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
            host = record.name.split('.', 1)[1]
            return _HOST_TO_SOURCE.get(host, host)
        return 'General'


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
