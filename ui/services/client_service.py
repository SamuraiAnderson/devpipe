from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


class Platform(Enum):
    LOCAL = "Local"
    LINUX = "Linux"
    ANDROID = "Android"
    SERIAL = "Serial"


class ConnState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ClientInfo:
    platform: Platform
    host: str
    user: str = ""
    state: ConnState = ConnState.DISCONNECTED
    error_msg: str = ""
    controller: object = field(default=None, repr=False)


class ClientService:
    def __init__(self):
        self._clients: dict[Platform, ClientInfo] = {
            Platform.LOCAL: ClientInfo(
                platform=Platform.LOCAL,
                host="localhost",
                user=os.name,
            ),
            Platform.LINUX: ClientInfo(
                platform=Platform.LINUX,
                host=os.getenv("LINUX_HOST", ""),
                user=os.getenv("LINUX_USER", ""),
            ),
            Platform.ANDROID: ClientInfo(
                platform=Platform.ANDROID,
                host=os.getenv("ANDROID_HOST", ""),
            ),
            Platform.SERIAL: ClientInfo(
                platform=Platform.SERIAL,
                host=os.getenv("SERIAL_PORT", ""),
            ),
        }
        self._try_connect_local()

    @property
    def clients(self) -> dict[Platform, ClientInfo]:
        return self._clients

    def _try_connect_local(self):
        from src.localhost import LocalHost
        try:
            ctrl = LocalHost()
            info = self._clients[Platform.LOCAL]
            info.controller = ctrl
            info.state = ConnState.CONNECTED
        except Exception as exc:
            info = self._clients[Platform.LOCAL]
            info.state = ConnState.ERROR
            info.error_msg = str(exc)

    def connect(self, platform: Platform) -> bool:
        info = self._clients[platform]
        try:
            if platform == Platform.LINUX:
                from src.linux_controller import Linux
                ctrl = Linux(info.host, info.user)
                info.controller = ctrl
            elif platform == Platform.ANDROID:
                from src.adb_connector import AdbCnet
                ctrl = AdbCnet(info.host)
                info.controller = ctrl
            elif platform == Platform.SERIAL:
                from src.serial_controller import SerialControl
                baudrate = int(os.getenv("SERIAL_BAUDRATE", "115200"))
                ctrl = SerialControl(info.host, baudrate=baudrate)
                info.controller = ctrl
            elif platform == Platform.LOCAL:
                self._try_connect_local()
                return self._clients[Platform.LOCAL].state == ConnState.CONNECTED
            info.state = ConnState.CONNECTED
            info.error_msg = ""
            log.info("已连接 %s (%s)", platform.value, info.host)
            return True
        except Exception as exc:
            info.state = ConnState.ERROR
            info.error_msg = str(exc)
            log.error("连接 %s 失败: %s", platform.value, exc)
            return False

    def disconnect(self, platform: Platform):
        info = self._clients[platform]
        if info.controller is not None:
            try:
                info.controller.close()
            except Exception:
                pass
            info.controller = None
        info.state = ConnState.DISCONNECTED
        info.error_msg = ""
        log.info("已断开 %s", platform.value)

    def get_controller(self, platform: Platform) -> Optional[object]:
        info = self._clients[platform]
        if info.state == ConnState.CONNECTED:
            return info.controller
        from src.BaseControl import BaseControl
        return BaseControl._registry.get(platform.value)

    def get_controller_by_key(self, key: str) -> Optional[object]:
        """通过 ``'platform.host'`` 格式的 key 查找控制器。"""
        for info in self._clients.values():
            ctrl = info.controller
            if ctrl and f"{info.platform.value}.{getattr(ctrl, 'host', '')}" == key:
                return ctrl
        from src.BaseControl import BaseControl
        return BaseControl._registry.get(key)
