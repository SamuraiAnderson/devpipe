"""通用工具：双通道日志缓冲与失败现场落盘。"""

import logging
import threading
from datetime import datetime
from pathlib import Path


class BufferHandler(logging.Handler):
    """只追加、不 flush 的 logging.Handler。

    与 ``MemoryHandler`` 不同，后者在 ERROR 级别会 flush 并清空 buffer，
    导致正常运行时终端无输出、失败时现场丢失。本 handler 与控制台 handler
    并行挂载，实时输出与缓冲互不干扰。
    """

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self._lock = threading.Lock()
        self._buffer: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        try:
            with self._lock:
                self._buffer.append(record)
        except Exception:
            self.handleError(record)

    def flush(self):
        """刻意不转发、不清空 buffer。"""

    def snapshot(self) -> list[logging.LogRecord]:
        """返回当前 buffer 的快照副本，供落盘或诊断使用。"""
        with self._lock:
            return list(self._buffer)


def save_buffered_log(
    handler: BufferHandler,
    prefix: str,
    log_dir: Path | str | None = None,
) -> Path:
    """失败时将 buffer 写入 ``logs/{prefix}_{timestamp}.log``，保留完整现场。

    写入完成后不清空 handler buffer，便于调用方继续查看或重复落盘。
    返回生成的日志文件路径。
    """
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent / 'logs'
    else:
        log_dir = Path(log_dir)

    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    safe_prefix = prefix.replace('/', '_').replace('\\', '_')
    path = log_dir / f'{safe_prefix}_{ts}.log'

    formatter = handler.formatter or logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s'
    )
    records = handler.snapshot()
    with open(path, 'w', encoding='utf-8') as f:
        for record in records:
            f.write(formatter.format(record) + '\n')

    return path
