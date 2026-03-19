import logging
from .BaseControl import *
import paramiko
from paramiko import SSHClient

class Linux(BaseControl):
    def __init__(self, host, user) -> None:
        self._user = user
        self._host = host
        self._pwd = '~'

        self.remoter = SSHClient()
        self.remoter.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.remoter.connect(hostname=self._host, username=self.user)

    @property
    def args_prefix(self):
        return ['cd', self.pwd, "&&"]

    @property
    def user(self):
        return self._user

    @property
    def host(self):
        return self._host

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

    def shell(self, *args):
        args = self.args_prefix + list(args)
        remote_cmd = args_to_cmd(args)
        stdin, stdout, stderr = self.remoter.exec_command(remote_cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise ShellError(self.host, remote_cmd, exit_status, stdout.read().decode(), stderr.read().decode())
        logging.debug("%s:%s", self.host, remote_cmd)
        return exit_status, stdout.read().decode(), stderr.read().decode()

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
        logging.debug("%s:push %s %s", self.host, local_path, remote_path)

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
        logging.debug("%s:pull %s %s", self.host, remote_path, local_path)

    def close(self):
        self.remoter.close()

    @property
    def name(self):
        return 'posix'

    def get_file_timestamp(self, path):
        code, stdout, _ = self.shell("stat", "-c", "%Y", path)
        return int(stdout)

    def file_exist(self, path):
        code, stdout, stderr = self.shell(f"test -e {path} && echo true || echo false")
        return stdout.strip() == "true" # TODO: 莫名奇妙回车符

