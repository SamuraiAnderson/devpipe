"""自包含的 TFTP 服务端，基于 tftpy 库实现。

在后台线程中运行 TFTP 服务，支持上下文管理器，用于嵌入式开发中
向设备提供固件/文件下载。
"""

import logging
import threading
from pathlib import Path

import tftpy

log = logging.getLogger(__name__)


class TftpdServer:
    """TFTP 服务端，封装 tftpy.TftpServer。

    Parameters
    ----------
    root_dir : str
        TFTP 根目录，客户端请求的文件相对于此目录解析。
    host : str
        监听地址，默认 ``"0.0.0.0"``（所有网卡）。
    port : int
        监听端口，默认 ``69``。低于 1024 的端口在 Linux 上需要 root 权限，
        Windows 上需要管理员权限；测试时可使用高端口（如 6969）。
    """

    def __init__(self, root_dir: str, host: str = "0.0.0.0", port: int = 69):
        self.root_dir = str(Path(root_dir).resolve())
        self.host = host
        self.port = port
        self._server: tftpy.TftpServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        """在后台线程中启动 TFTP 服务。"""
        root = Path(self.root_dir)
        if not root.is_dir():
            root.mkdir(parents=True, exist_ok=True)
            log.info("已创建 TFTP 根目录: %s", self.root_dir)

        self._server = tftpy.TftpServer(self.root_dir)
        self._thread = threading.Thread(
            target=self._server.listen,
            kwargs={"listenip": self.host, "listenport": self.port},
            daemon=True,
        )
        self._thread.start()
        log.info("TFTP 服务已启动  %s:%d  root=%s", self.host, self.port, self.root_dir)

    def stop(self):
        """停止 TFTP 服务并等待线程退出。"""
        if self._server is not None:
            self._server.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        log.info("TFTP 服务已停止")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
