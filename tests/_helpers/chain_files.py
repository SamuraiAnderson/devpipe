"""Filesystem and device-tool boundary simulation for staged transfer contracts."""
import errno
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from .chain_host import Reply, install_host


def install_files(monkeypatch, platform="posix", kind="adb"):
    wire = install_host(monkeypatch, platform)
    wire.files = {}
    wire.device = b""
    wire.cleanups = 0
    wire.sftp_failure = None
    wire.device_failure = False
    wire.cleanup_failure = False
    wire.delay = 0
    wire.timeouts = []
    wire.directory = "C:\\Temp Space\\redpymake-test" if platform == "windows" else "/tmp/space/redpymake-test"
    staging = wire.directory.replace("\\", "/") + "/payload"

    class Sftp:
        def __init__(self): self.timeout = None
        def get_channel(self): return self
        def gettimeout(self): return self.timeout
        def settimeout(self, value):
            self.timeout = value
            wire.timeouts.append(value)
        def stat(self, path):
            if wire.sftp_failure: raise wire.sftp_failure
            if path not in wire.files: raise FileNotFoundError(errno.ENOENT, "missing", path)
            return SimpleNamespace(st_size=len(wire.files[path]), st_mtime=100, st_mode=stat.S_IFREG)
        def put(self, local, remote, callback=None):
            if wire.sftp_failure: raise wire.sftp_failure
            assert "\\" not in remote
            data = Path(local).read_bytes()
            time.sleep(wire.delay)
            if callback: callback(len(data), len(data))
            wire.files[remote] = data
            return self.stat(remote)
        def get(self, remote, local, callback=None):
            if wire.sftp_failure: raise wire.sftp_failure
            assert "\\" not in remote
            data = wire.files[remote]
            time.sleep(wire.delay)
            if callback: callback(len(data), len(data))
            Path(local).write_bytes(data)
        def close(self): pass

    import sys
    monkeypatch.setattr(sys.modules["paramiko"].SSHClient, "open_sftp", lambda self: Sftp(), raising=False)
    monkeypatch.setattr(sys.modules["paramiko"], "SFTPClient", lambda channel: Sftp(), raising=False)

    def handler(command):
        if "mktemp" in command or "GetTempPath" in command:
            return Reply(wire.directory.encode())
        if "rm -rf --" in command or "Remove-Item -LiteralPath" in command:
            wire.cleanups += 1
            if wire.cleanup_failure: return Reply(err=b"cleanup denied", rc=1)
            wire.files.clear()
            return Reply()
        if "wslpath" in command:
            return Reply(b"/mounted/temp/payload")
        if "[ -e" in command:
            return Reply(b"no")
        is_push = " push " in command or ("cp -f -- /mounted/temp/payload" in command)
        is_pull = " pull " in command or ("cp -f -- /device/payload /mounted/temp/payload" in command)
        if is_push or is_pull:
            if wire.device_failure: return Reply(err=b"device transfer denied", rc=1)
            if is_push: wire.device = wire.files[staging]
            else: wire.files[staging] = wire.device
            return Reply()
        return None
    wire.handler = handler
    return wire
