from .BaseControl import *
import re
import subprocess
import time

class AdbCnet(BaseControl):
    def __init__(self, host, user=None) -> None:
        self._host:str = host
        self._pwd = '~'
        self._user = 'oem'
        self._connect()
        self._register()

    @property
    def platform(self) -> str:
        return "Android"

    @property
    def args_prefix(self):
        return ['cd', self.pwd, "&&"]

    def cd(self, target):
        resolve_cmd = args_to_cmd(["adb", "shell",
            f"cd {self._pwd} && cd {target} && pwd"])
        code, out, _ = subprocess_run(resolve_cmd)
        if code != 0:
            raise RemoteFileNotFoundError(self.host, target)
        self._pwd = out.decode(errors='replace').strip()

    @property
    def pwd(self):
        return self._pwd

    @pwd.setter
    def pwd(self, path):
        code, *_ = self.shell(f"[ -d '{path}' ]")
        if code == 0:
            self._pwd = path
        else:
            raise RemoteFileNotFoundError(self.host, path)

    @property
    def user(self):
        return self._user

    @property
    def host(self):
        return self._host

    def _connect(self):
        args = ['adb', 'connect', self._host]
        cmd = args_to_cmd(args)
        code, out, err = subprocess_run(cmd)
        if code != 0:
            raise RemoteConnectionError(self._host, err)

    def shell(self, *args) -> tuple:
        args = self.args_prefix + list(args)
        remote_cmd = args_to_cmd(args)
        cmd = args_to_cmd(["adb", "shell", remote_cmd])
        code, out, err = subprocess_run_streaming(
            cmd, on_chunk=self._log_chunk
        )
        if code != 0:
            raise ShellError(self.host, remote_cmd, code, out, err)
        self.log.debug("shell: %s", cmd)
        return code, out.decode(errors='replace'), err.decode(errors='replace')

    def wait(self, cmd, pattern, timeout=30):
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        args = self.args_prefix + [cmd]
        remote_cmd = args_to_cmd(args)
        full_cmd = args_to_cmd(["adb", "shell", remote_cmd])
        process = subprocess.Popen(
            full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
        )

        deadline = time.time() + timeout
        output, match = self._stream_and_tee(
            subprocess_read_fn(process, deadline),
            stop_fn=lambda acc: pattern.search(acc),
        )
        if match:
            process.kill()
            return match
        raise WaitTimeoutError(self.host, pattern.pattern, timeout, output)

    def push(self, file_local, file_remote):
        cmd = args_to_cmd(['adb', 'push', file_local, file_remote])
        code, out, err = subprocess_run(cmd)
        if code != 0:
            raise TransferError(self.host, file_local, file_remote, err)
        self.log.debug("push %s → %s", file_local, file_remote)

    def pull(self, file_remote, file_local):
        cmd = args_to_cmd(['adb', 'pull', file_remote, file_local])
        code, out, err = subprocess_run(cmd)
        if code != 0:
            raise TransferError(self.host, file_remote, file_local, err)
        self.log.debug("pull %s → %s", file_remote, file_local)

    def open_interactive(self) -> 'InteractiveSession':
        import subprocess as _sp
        process = _sp.Popen(
            ['adb', 'shell'],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.STDOUT,
        )
        return SubprocessInteractiveSession(process)

    def close(self):
        cmd = args_to_cmd(['adb', 'disconnect', self._host])
        subprocess_run(cmd)
        self._unregister()

    @property
    def name(self):
        return 'posix'

    def get_file_timestamp(self, path):
        code, stdout, _ = self.shell("stat", "-c", "%Y", path)
        return int(stdout)

    def file_exist(self, path):
        code, stdout, stderr = self.shell(f"test -e {path} && echo true || echo false")
        return stdout.strip() == "true"
