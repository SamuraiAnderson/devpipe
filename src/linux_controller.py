from .BaseControl import *
import re
import time
import paramiko
from paramiko import SSHClient

class _ParamikoInteractiveSession(InteractiveSession):
    """基于 Paramiko SSH Channel 的交互式会话。"""

    def __init__(self, channel):
        self._channel = channel

    def write(self, data: bytes) -> None:
        if not self._channel.closed:
            self._channel.send(data)

    def read_nonblocking(self) -> bytes | None:
        if self._channel.closed:
            return None
        if self._channel.recv_ready():
            return self._channel.recv(4096)
        if self._channel.exit_status_ready() and not self._channel.recv_ready():
            return None
        return None

    def resize(self, cols: int, rows: int) -> None:
        if not self._channel.closed:
            self._channel.resize_pty(width=cols, height=rows)

    def close(self) -> None:
        if not self._channel.closed:
            self._channel.close()

    @property
    def closed(self) -> bool:
        return self._channel.closed


class Linux(BaseControl):
    def __init__(self, host, user) -> None:
        self._user = user
        self._host = host
        self._pwd = '~'

        self.remoter = SSHClient()
        self.remoter.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.remoter.connect(hostname=self._host, username=self.user)
        self._register()

    @property
    def platform(self) -> str:
        return "Linux"

    @property
    def args_prefix(self):
        return ['cd', self.pwd, "&&"]

    @property
    def user(self):
        return self._user

    @property
    def host(self):
        return self._host

    def cd(self, target):
        resolve_cmd = f"cd {self._pwd} && cd {target} && pwd"
        stdin, stdout, stderr = self.remoter.exec_command(resolve_cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise RemoteFileNotFoundError(self.host, target)
        self._pwd = stdout.read().decode(errors='replace').strip()

    @property
    def pwd(self):
        return self._pwd

    @pwd.setter
    def pwd(self, path):
        code, *_ = self.shell(f"test -d {path}")
        if code == 0:
            self._pwd = path
        else:
            raise RemoteFileNotFoundError(self.host, path)

    @staticmethod
    def _channel_read_fn(channel):
        """返回一个从 paramiko Channel 流式读取的 read_fn 闭包。"""
        def read_fn():
            if channel.recv_ready():
                return channel.recv(4096).decode(errors='replace')
            if channel.exit_status_ready() and not channel.recv_ready():
                return None
            return ""
        return read_fn

    def shell(self, *args):
        args = self.args_prefix + list(args)
        remote_cmd = args_to_cmd(args)
        stdin, stdout, stderr = self.remoter.exec_command(remote_cmd)
        channel = stdout.channel

        out_text, _ = self._stream_and_tee(self._channel_read_fn(channel))
        exit_status = channel.recv_exit_status()
        stderr_text = stderr.read().decode(errors='replace')
        if exit_status != 0:
            raise ShellError(self.host, remote_cmd, exit_status, out_text, stderr_text)
        self.log.debug("shell: %s", remote_cmd)
        return exit_status, out_text, stderr_text

    def wait(self, cmd, pattern, timeout=30):
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        args = self.args_prefix + [cmd]
        remote_cmd = args_to_cmd(args)
        stdin, stdout, stderr = self.remoter.exec_command(remote_cmd)
        channel = stdout.channel

        deadline = time.time() + timeout

        def read_fn():
            if time.time() >= deadline:
                channel.close()
                return None
            if channel.recv_ready():
                return channel.recv(4096).decode(errors='replace')
            if channel.exit_status_ready() and not channel.recv_ready():
                return None
            return ""

        output, match = self._stream_and_tee(
            read_fn,
            stop_fn=lambda acc: pattern.search(acc),
        )
        if match:
            channel.close()
            return match
        raise WaitTimeoutError(self.host, pattern.pattern, timeout, output)

    def push(self, local_path, remote_path):
        sftp = None
        try:
            sftp = self.remoter.open_sftp()
            sftp.put(local_path, remote_path)
        except Exception as e:
            raise TransferError(self.host, local_path, remote_path, str(e)) from e
        finally:
            if sftp is not None:
                sftp.close()
        self.log.debug("push %s → %s", local_path, remote_path)

    def pull(self, remote_path, local_path):
        sftp = None
        try:
            sftp = self.remoter.open_sftp()
            sftp.get(remote_path, local_path)
        except Exception as e:
            raise TransferError(self.host, remote_path, local_path, str(e)) from e
        finally:
            if sftp is not None:
                sftp.close()
        self.log.debug("pull %s → %s", remote_path, local_path)

    def open_interactive(self) -> 'InteractiveSession':
        transport = self.remoter.get_transport()
        channel = transport.open_session()
        channel.get_pty(term='xterm-256color', width=120, height=40)
        channel.invoke_shell()
        return _ParamikoInteractiveSession(channel)

    def close(self):
        self.remoter.close()
        self._unregister()

    @property
    def name(self):
        return 'posix'

    def get_file_timestamp(self, path):
        code, stdout, _ = self.shell("stat", "-c", "%Y", path)
        return int(stdout)

    def file_exist(self, path):
        code, stdout, stderr = self.shell(f"test -e {path} && echo true || echo false")
        return stdout.strip() == "true" # TODO: 莫名奇妙回车符

