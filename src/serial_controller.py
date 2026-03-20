from .BaseControl import *
import re
import time
import base64
import uuid


class SerialControl(BaseControl):
    """通过串口 (UART) 控制远程设备。

    支持两种模式：
    - 'linux'（默认）：Linux shell，使用 base64 编解码实现文件传输，
      通过唯一标记符界定命令输出边界。
    - 'bootloader'：U-Boot 模式，直接发送命令，通过提示符检测响应结束。
    """

    CHUNK_SIZE = 512

    def __init__(self, port, baudrate=115200, timeout=10, mode='linux'):
        import serial as _serial
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._mode = mode
        self._pwd = '~'
        try:
            self._ser = _serial.Serial(port, baudrate, timeout=timeout)
        except Exception as e:
            reason = self._parse_open_error(e)
            raise RemoteConnectionError(port, reason) from e
        self.log.info("串口 %s 打开成功 (mode=%s)", port, mode)
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        self._exec_raw("")
        if mode == 'linux':
            self._exec_raw("stty -echo 2>/dev/null; export PS1=''")

    @staticmethod
    def _parse_open_error(exc):
        msg = str(exc).lower()
        if 'errno 13' in msg or 'access is denied' in msg:
            return "权限被拒绝：串口可能被其他程序占用，或当前用户无访问权限"
        if 'errno 2' in msg or 'filenotfounderror' in msg:
            return "串口不存在：请检查端口名称是否正确，设备是否已连接"
        if 'errno 32' in msg or 'broken pipe' in msg:
            return "资源繁忙：串口已被其他进程占用"
        if 'errno 183' in msg:
            return "无法创建串口实例：端口名称冲突或已被占用"
        if 'permission' in msg:
            return f"权限错误：{exc}"
        return f"串口打开失败：{exc}"

    @property
    def host(self):
        return self._port

    @property
    def args_prefix(self):
        return ['cd', self.pwd, '&&']

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

    @property
    def name(self):
        return 'posix'

    def _read_until(self, sentinel, deadline):
        buf = ""
        while time.time() < deadline:
            n = self._ser.in_waiting
            if n:
                buf += self._ser.read(n).decode(errors='replace')
                if sentinel in buf:
                    return buf
            else:
                time.sleep(0.02)
        raise WaitTimeoutError(self.host, sentinel, self._timeout, buf)

    def _exec_raw(self, cmd):
        if self._mode == 'bootloader':
            return self._exec_raw_bootloader(cmd)
        return self._exec_raw_linux(cmd)

    def _exec_raw_linux(self, cmd):
        """发送命令并通过标记符解析输出和退出码。"""
        marker = uuid.uuid4().hex[:16]
        start_tag = f"_S{marker}_"
        end_tag = f"_E{marker}_"

        wrapped = (
            f"echo {start_tag}; {cmd}; _rc=$?; echo; "
            f"echo _RC${{_rc}}RC_; echo {end_tag}\n"
        )
        self._ser.reset_input_buffer()
        self._ser.write(wrapped.encode())
        self._ser.flush()

        deadline = time.time() + self._timeout
        raw = self._read_until(end_tag, deadline)

        start_pos = raw.find(start_tag)
        end_pos = raw.find(end_tag)
        if start_pos == -1 or end_pos == -1:
            return -1, raw, ""

        body = raw[start_pos + len(start_tag):end_pos]
        rc_match = re.search(r'_RC(\d+)RC_', body)
        code = int(rc_match.group(1)) if rc_match else -1
        stdout = re.sub(r'\n?_RC\d+RC_\n?', '', body).strip()

        return code, stdout, ""

    def _exec_raw_bootloader(self, cmd):
        """Bootloader 模式：直接发送命令，检测提示符结束。"""
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\n").encode())
        self._ser.flush()

        buf = ""
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            n = self._ser.in_waiting
            if n:
                buf += self._ser.read(n).decode(errors='replace')
                lines = buf.rstrip().split('\n')
                if lines:
                    last = lines[-1].strip()
                    if last.endswith('#') or last.endswith('>'):
                        stdout = '\n'.join(lines[1:-1]).strip()
                        return 0, stdout, ""
            else:
                time.sleep(0.02)
        raise WaitTimeoutError(self.host, "prompt (# or >)", self._timeout, buf)

    def shell(self, *args) -> tuple:
        if self._mode == 'bootloader':
            return self._shell_bootloader(*args)
        return self._shell_linux(*args)

    def _shell_linux(self, *args) -> tuple:
        args = self.args_prefix + list(args)
        remote_cmd = args_to_cmd(args)
        code, stdout, stderr = self._exec_raw(remote_cmd)
        if code != 0:
            raise ShellError(self.host, remote_cmd, code, stdout, stderr)
        self.log.debug("shell: %s", remote_cmd)
        return code, stdout, stderr

    def _shell_bootloader(self, *args) -> tuple:
        remote_cmd = args_to_cmd(list(args))
        code, stdout, stderr = self._exec_raw(remote_cmd)
        if code != 0:
            raise ShellError(self.host, remote_cmd, code, stdout, stderr)
        self.log.debug("shell: %s", remote_cmd)
        return code, stdout, stderr

    def push(self, local_path, remote_path):
        try:
            with open(local_path, 'rb') as f:
                data = f.read()
            encoded = base64.b64encode(data).decode()
            chunks = [encoded[i:i + self.CHUNK_SIZE]
                      for i in range(0, len(encoded), self.CHUNK_SIZE)]
            self._exec_raw(f"echo -n '{chunks[0]}' > /tmp/_ser_xfer")
            for chunk in chunks[1:]:
                self._exec_raw(f"echo -n '{chunk}' >> /tmp/_ser_xfer")
            self._exec_raw(f"base64 -d /tmp/_ser_xfer > {remote_path}")
            self._exec_raw("rm -f /tmp/_ser_xfer")
        except TransferError:
            raise
        except Exception as e:
            raise TransferError(self.host, local_path, remote_path, str(e)) from e
        self.log.debug("push %s → %s", local_path, remote_path)

    def pull(self, remote_path, local_path):
        try:
            code, stdout, _ = self._exec_raw(f"base64 {remote_path}")
            if code != 0:
                raise TransferError(self.host, remote_path, local_path, stdout)
            data = base64.b64decode(stdout.replace('\n', '').replace('\r', ''))
            with open(local_path, 'wb') as f:
                f.write(data)
        except TransferError:
            raise
        except Exception as e:
            raise TransferError(self.host, remote_path, local_path, str(e)) from e
        self.log.debug("pull %s → %s", remote_path, local_path)

    def wait(self, cmd, pattern, timeout=30):
        """执行 cmd 并流式监控输出，直到匹配 pattern 或超时。

        linux 模式下会自动添加 cd 前缀；bootloader 模式直接发送命令。
        pattern: str 或 re.Pattern，支持捕获组。
        返回 re.Match，可通过 match.group(1) 等提取变量。
        """
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        if self._mode == 'linux':
            args = self.args_prefix + [cmd]
            remote_cmd = args_to_cmd(args)
        else:
            remote_cmd = cmd
        self._ser.write((remote_cmd + "\n").encode())
        self._ser.flush()

        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = self._ser.in_waiting
            if n:
                chunk = self._ser.read(n).decode(errors='replace')
                output += chunk
                m = pattern.search(output)
                if m:
                    return m
            else:
                time.sleep(0.05)
        raise WaitTimeoutError(self.host, pattern.pattern, timeout, output)

    def get_file_timestamp(self, path):
        code, stdout, _ = self.shell("stat", "-c", "%Y", path)
        return int(stdout)

    def file_exist(self, path):
        code, stdout, stderr = self.shell(
            f"test -e {path} && echo true || echo false"
        )
        return stdout.strip() == "true"

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
