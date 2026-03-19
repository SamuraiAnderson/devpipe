from .BaseControl import *
import re
import time
import base64
import uuid


class SerialControl(BaseControl):
    """通过串口 (UART) 控制远程 Linux 设备。

    使用 base64 编解码实现文件传输，通过唯一标记符界定命令输出边界。
    """

    CHUNK_SIZE = 512

    def __init__(self, port, baudrate=115200, timeout=10):
        import serial as _serial
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._pwd = '~'
        self._ser = _serial.Serial(port, baudrate, timeout=timeout)
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        self._exec_raw("")
        self._exec_raw("stty -echo 2>/dev/null; export PS1=''")

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

    def shell(self, *args) -> tuple:
        args = self.args_prefix + list(args)
        remote_cmd = args_to_cmd(args)
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
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        args = self.args_prefix + [cmd]
        remote_cmd = args_to_cmd(args)
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
