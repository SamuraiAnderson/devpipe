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
    _registry: dict[str, 'BaseControl'] = {}
    _executor = __import__('concurrent.futures', fromlist=['ThreadPoolExecutor']).ThreadPoolExecutor(max_workers=8)

    @property
    def platform(self) -> str:
        raise NotImplementedError

    @property
    def log(self):
        import logging
        return logging.getLogger(f"client.{self.platform}.{self.host}")

    def _register(self):
        BaseControl._registry[self.platform] = self

    def _unregister(self):
        if BaseControl._registry.get(self.platform) is self:
            del BaseControl._registry[self.platform]

    def _log_chunk(self, chunk):
        for line in chunk.splitlines():
            if line:
                self.log.info(line)

    def _stream_and_tee(self, read_fn, on_chunk=None, stop_fn=None, interval=0.05):
        """流式读取 + 日志分发的通用模板。

        read_fn() -> str | None
            返回 str（含空串表示暂无数据）= 流仍在；返回 None = EOF。
        on_chunk(chunk)
            每读到非空 chunk 时回调，默认 self._log_chunk。
        stop_fn(accumulated) -> truthy | None
            每次累加后调用，返回 truthy 则提前终止并作为第二返回值。
        interval
            无数据时的 sleep 秒数。

        Returns: (accumulated_output, stop_result | None)
        """
        import time
        if on_chunk is None:
            on_chunk = self._log_chunk
        output = ""
        while True:
            chunk = read_fn()
            if chunk is None:
                break
            if chunk:
                output += chunk
                on_chunk(chunk)
                if stop_fn is not None:
                    result = stop_fn(output)
                    if result:
                        return output, result
            else:
                time.sleep(interval)
        return output, None

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

    def cd(self, target: str):
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

    # ── async 包装：不同连接间可并发，同一连接由调用方保证串行 ──

    async def async_shell(self, *args, **kwargs) -> tuple:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.shell(*args, **kwargs))

    async def async_wait(self, cmd, pattern, timeout=30):
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.wait(cmd, pattern, timeout))

    async def async_push(self, local_path, remote_path):
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.push(local_path, remote_path))

    async def async_pull(self, remote_path, local_path):
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.pull(remote_path, local_path))

    async def async_cd(self, target: str):
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.cd(target))

    # ── 生命周期 ──

    def close(self):
        self._unregister()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
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

def subprocess_run_streaming(cmd, on_chunk=None, cwd=None):
    """流式版 subprocess_run，边读边回调 on_chunk(str)。"""
    import subprocess
    process = subprocess.Popen(cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                cwd=cwd
            )
    output = b""
    while True:
        chunk = process.stdout.read1(4096)
        if not chunk:
            break
        output += chunk
        if on_chunk:
            on_chunk(chunk.decode(errors='replace'))
    stderr = process.stderr.read()
    process.wait()
    return (process.returncode, output, stderr)


def subprocess_read_fn(process, deadline=None):
    """为 subprocess 返回非阻塞的 read_fn 闭包（内部用后台线程读取）。"""
    import os
    import threading
    import queue as _queue

    q = _queue.Queue()

    def _reader():
        fd = process.stdout.fileno()
        try:
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                q.put(data)
        except OSError:
            pass
        q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    def read_fn():
        if deadline is not None:
            import time
            if time.time() >= deadline:
                process.kill()
                return None
        try:
            data = q.get(timeout=0.1)
            if data is None:
                return None
            return data.decode(errors='replace')
        except _queue.Empty:
            return ""

    return read_fn

