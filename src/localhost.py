from .BaseControl import *
import os

class LocalHost(BaseControl):
    def __init__(self) -> None:
        self._pwd = os.getcwd()

    @property
    def pwd(self):
        return self._pwd

    @pwd.setter
    def pwd(self, path):
        if os.path.exists(path) and os.path.isdir(path):
            self._pwd = path
        else:
            raise RemoteFileNotFoundError("localhost", path)

    @property
    def name(self):
        return os.name

    @property
    def host(self):
        return "localhost"

    def get_file_timestamp(self, path):
        return int(os.stat(path).st_mtime)

    def file_exist(self, path):
        return os.path.exists(path)