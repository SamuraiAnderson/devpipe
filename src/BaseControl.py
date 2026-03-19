class RemoteError(Exception):
    def __init__(self, host, message):
        self.host = host
        super().__init__(f"[{host}] {message}")

class ShellError(RemoteError):
    def __init__(self, host, cmd, code, stdout="", stderr=""):
        self.cmd = cmd
        self.code = code
        super().__init__(host, f"command failed (exit {code}): {cmd}\nstdout: {stdout}\nstderr: {stderr}")

class TransferError(RemoteError):
    def __init__(self, host, src, dst, reason=""):
        super().__init__(host, f"transfer failed: {src} -> {dst}. {reason}")

class RemoteConnectionError(RemoteError):
    def __init__(self, host, reason=""):
        super().__init__(host, f"connection failed. {reason}")

class RemoteFileNotFoundError(RemoteError):
    def __init__(self, host, path, reason=""):
        self.path = path
        super().__init__(host, f"path not found: {path}. {reason}")

class WaitTimeoutError(RemoteError):
    def __init__(self, host, pattern, timeout, output=""):
        self.pattern = pattern
        self.timeout = timeout
        self.output = output
        super().__init__(host, f"wait for '{pattern}' timed out after {timeout}s")


class BaseControl(object):
    '''
        android rv1106 时间会快一点
    '''
    @property
    def log(self):
        import logging
        return logging.getLogger(f"client.{self.host}")

    def push(self, local_path, remote_path):
        raise NotImplementedError()

    def pull(self, remote_path, local_path):
        raise NotImplementedError()

    def shell(self, *args, **kwargs) -> tuple:
        raise NotImplementedError()

    def wait(self, cmd, pattern, timeout=30):
        '''执行 cmd，流式监控输出直到匹配 pattern(正则) 或超时。
        pattern: str 或 re.Pattern，支持捕获组。
        返回 re.Match，可通过 match.group(1) 等提取变量。
        '''
        raise NotImplementedError()

    def pwd(self):
        raise NotImplementedError()

    def name(self):
        raise NotImplementedError()

    def home(self):
        code, stdout, stderr = self.shell("echo $HOME")
        return stdout

    def get_file_timestamp(self, file):
        raise NotImplementedError()

    def file_exist(self, path):
        raise NotImplementedError()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def args_to_cmd(args_li):
    cmd = ' '.join(args_li)
    return cmd 

def subprocess_run(cmd):
    import subprocess
    process = subprocess.Popen(cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                shell=True
            )
    stdout, stderr = process.communicate()
    return (process.returncode, 
            stdout,
            stderr
        )

