import logging
import os
from datetime import datetime
from typing import Callable, Optional

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


class UILogHandler(logging.Handler):
    """Routes Python logging records to per-client UI callbacks."""

    _IGNORED_LOGGERS = frozenset((
        'nicegui', 'uvicorn', 'watchfiles', 'httpx',
        'asyncio', 'engineio', 'socketio',
    ))

    def __init__(self):
        super().__init__()
        self._callbacks: dict[str, Callable[[LogRecord], None]] = {}
        self._emitting = False
        self.setFormatter(logging.Formatter("%(message)s"))

    def register(self, source: str, fn: Callable[[LogRecord], None]):
        self._callbacks[source] = fn

    def emit(self, record: logging.LogRecord):
        if self._emitting or not self._callbacks:
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

            if ALL in self._callbacks:
                self._callbacks[ALL](lr)
            if source in self._callbacks:
                self._callbacks[source](lr)
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
