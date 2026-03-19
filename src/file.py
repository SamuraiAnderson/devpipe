from .BaseControl import BaseControl
from .path import *
import os
import sys

class UFile(object):
    '''
        @brief: 文件资源定位
    '''
    def __init__(self, dev:BaseControl, path:str) -> None:
        self._dev = dev
        self._path = path
        self._remote_old_pwd = None
        self._exist = False
        self.check_cache_right()
        
    def check_cache_right(self):
        if self.RemoteUser.pwd != self._remote_old_pwd:
            self._remote_old_pwd = self.RemoteUser.pwd
            self._exist = self._is_exist()

    @property
    def path(self):
        return self._path

    @property
    def RemoteUser(self):
        return self._dev

    @property
    def exist(self):
        self.check_cache_right()
        return self._exist

    def get_abs_path(self):
        if self.is_absolute_path(self._path):
            return self._path
        return abspath(self._dev.pwd, self._path, self._dev.name) # TODO:

    def is_absolute_path(self, path:str):
        if self._dev.name == 'nt':
            if os.name == 'nt':
                return os.path.isabs(path)
            return isabs_windows(path)
        else: # linux, mac
            return path.startswith('/') or path.startswith('~')

    def get_timestamp(self):
        if self.exist:
            return self._dev.get_file_timestamp(self._path)
        else:
            return None

    def is_file(self):
        if self.RemoteUser.name == 'nt':
            return os.path.isfile(self.path) # TODO:
        else:
            code, stdout, stderr = self.RemoteUser.shell(f"test -f {self.path}")
            return code == 0

    def is_dir(self):
        if self.RemoteUser.name == 'nt':
            return os.path.isdir(self.path) # TODO:
        else:
            code, stdout, stderr = self.RemoteUser.shell(f"test -d {self.path}")
            return code == 0


    def _is_exist(self):
        return self._dev.file_exist(self._path)

    def __str__(self) -> str:
        return f"PATH({self._dev.host}, {self._dev.pwd}, {self.path})"

    def __repr__(self) -> str:
        return self.__str__()
