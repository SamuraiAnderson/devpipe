from .BaseControl import *
import os
import re
import subprocess
import time


class LocalHost(BaseControl):
    def __init__(self) -> None:
        self._pwd = os.getcwd()
        self._register()

    @property
    def pwd(self):
        return self._pwd

    @pwd.setter
    def pwd(self, path):
        if os.path.exists(path) and os.path.isdir(path):
            self._pwd = path
        else:
            raise RemoteFileNotFoundError("localhost", path)

    def cd(self, target):
        target = os.path.expanduser(target)
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(self._pwd, target))
        self.pwd = target

    @property
    def platform(self) -> str:
        return "Local"

    @property
    def name(self):
        return os.name

    @property
    def host(self):
        return "localhost"

    def shell(self, *args) -> tuple:
        cmd = args_to_cmd(list(args))
        code, out, err = subprocess_run_streaming(
            cmd, on_chunk=self._log_chunk, cwd=self._pwd
        )
        stdout = out.decode(errors='replace') if isinstance(out, bytes) else out
        stderr = err.decode(errors='replace') if isinstance(err, bytes) else err
        if code != 0:
            raise ShellError(self.host, cmd, code, stdout, stderr)
        self.log.debug("shell: %s", cmd)
        return code, stdout, stderr

    def wait(self, cmd, pattern, timeout=30):
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=True, cwd=self._pwd,
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

    def get_file_timestamp(self, path):
        return int(os.stat(path).st_mtime)

    def file_exist(self, path):
        return os.path.exists(path)