"""SSH wire boundary stub. Tests exercise public sessions, not log internals."""
from __future__ import annotations

import base64
import re
import threading
import types
import sys
from dataclasses import dataclass


@dataclass
class Reply:
    out: bytes = b""
    err: bytes = b""
    rc: int = 0
    read_error: Exception | None = None
    chunk: int = 13


class Channel:
    def __init__(self, server):
        self.server = server
        self.closed = False
        self.reply = Reply()
        self._out = bytearray()
        self._err = bytearray()
        self._lock = threading.Lock()

    def settimeout(self, value):
        self.timeout = value

    def set_combine_stderr(self, value):
        assert value is False

    def invoke_subsystem(self, name):
        assert name == "sftp"

    def get_pty(self, *args, **kwargs):
        raise AssertionError("chained transports must never allocate a PTY")

    def exec_command(self, command):
        self.server.commands.append(command)
        self.reply = self.server.respond(command)
        self._out.extend(self.reply.out)
        self._err.extend(self.reply.err)

    def recv(self, count):
        if self.reply.read_error:
            raise self.reply.read_error
        return self._read(self._out, count)

    def recv_stderr(self, count):
        return self._read(self._err, count)

    def _read(self, buf, count):
        with self._lock:
            data = bytes(buf[:min(count, self.reply.chunk)])
            del buf[:len(data)]
            return data

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self.reply.rc

    def close(self):
        self.closed = True


class Server:
    def __init__(self, platform="posix"):
        self.platform = platform
        self.active = True
        self.commands = []
        self.channels = []
        self.clients = []
        self.handler = None
        self.fail_adb = False

    def respond(self, command):
        decoded = command
        if "-EncodedCommand " in command:
            decoded = base64.b64decode(command.split("-EncodedCommand ")[-1]).decode("utf-16le")
        if "__RPM_POSIX__" in decoded:
            return Reply(b"__RPM_POSIX__\nLinux\n/home/test\n") if self.platform == "posix" else Reply(err=b"not found", rc=127)
        if "__RPM_WINDOWS__" in decoded:
            return Reply(b"__RPM_WINDOWS__\nC:\\Users\\test\n") if self.platform == "windows" else Reply(err=b"not found", rc=127)
        if self.handler is not None:
            reply = self.handler(decoded)
            if reply is not None:
                return reply
        if "get-state" in decoded:
            return Reply(b"offline\n" if self.fail_adb else b"device\n", rc=1 if self.fail_adb else 0)
        if "__RPM_EXIT_" in decoded:
            marker = re.search(r"__RPM_EXIT_[\w-]+__", decoded).group()
            return Reply(("hello\n" + marker + "0\n").encode())
        if "Get-Command" in decoded:
            return Reply(b"C:\\Windows\\System32\\wsl.exe\n")
        if "__RPM_ALIVE__" in decoded:
            return Reply(b"__RPM_ALIVE__")
        if "command -v" in decoded:
            return Reply(b"/usr/bin/adb\n")
        if '"$HOME"' in decoded:
            return Reply(b"/home/test")
        return Reply()


def install_host(monkeypatch, platform="posix"):
    server = Server(platform)

    class Transport:
        def is_active(self):
            return server.active

        def open_session(self, **kwargs):
            channel = ProcessChannel(server) if getattr(server, "bridge", False) or getattr(server, "native", False) else Channel(server)
            server.channels.append(channel)
            return channel

        def set_keepalive(self, value):
            pass

    class Client:
        def __init__(self):
            self.closed = False
            self.transport = Transport()
            server.clients.append(self)

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def get_transport(self):
            return self.transport

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "paramiko", types.SimpleNamespace(SSHClient=Client, AutoAddPolicy=object))
    return server


_SERIAL_STUB = r'''
import sys, types, threading, time
class Port:
    def __init__(self, port, **kw):
        if port == 'fail-open': raise OSError('port denied')
        self.name = port
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.closed = False
    def read(self, n):
        if self.name == 'fail-read':
            time.sleep(.1)
            raise OSError('UART unplugged')
        with self.cv:
            if not self.buf: self.cv.wait(.05)
            data = bytes(self.buf[:n]); del self.buf[:n]
            return data
    @property
    def in_waiting(self):
        return len(self.buf)
    def write(self, data):
        part = bytes(data[:7])
        with self.cv:
            self.buf.extend(part); self.cv.notify_all()
        return len(part)
    def flush(self): pass
    def close(self): self.closed = True
sys.modules['serial'] = types.SimpleNamespace(Serial=Port)
'''


class ProcessChannel(Channel):
    """Actual standalone bridge subprocess over deliberately fragmented SSH IO."""
    def exec_command(self, command):
        import shlex
        import subprocess
        import queue
        import socket
        import os
        args = shlex.split(command)
        native = getattr(self.server, 'native', False) and '-EncodedCommand ' in command and '$p=New-Object' in base64.b64decode(command.split('-EncodedCommand ')[-1]).decode('utf-16le')
        if not native and (len(args) < 4 or '-c' not in args or '<rpm-uart>' not in command):
            self.process = None
            return super().exec_command(command)
        self.server.commands.append(command)
        if native:
            launch = command
        else:
            code_index = args.index('-c') + 1
            launch = [sys.executable, '-u', '-c', _SERIAL_STUB + '\n' + args[code_index], *args[code_index + 1:]]
        self.process = subprocess.Popen(launch,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=dict(os.environ), bufsize=0)
        self.queues = [queue.Queue(), queue.Queue()]
        self.buffers = [bytearray(), bytearray()]
        self.eof = [False, False]
        self.pumps = []
        def pump(index, stream):
            try:
                while data := stream.read(1024):
                    self.queues[index].put(data)
            finally:
                self.queues[index].put(None)
        for i, stream in enumerate((self.process.stdout, self.process.stderr)):
            thread = threading.Thread(target=pump, args=(i, stream), daemon=True)
            thread.start()
            self.pumps.append(thread)

    def _receive(self, index, count):
        import queue
        import socket
        if self.process is None:
            return super().recv_stderr(count) if index else super().recv(count)
        if not self.buffers[index] and not self.eof[index]:
            try:
                chunk = self.queues[index].get(timeout=.05)
            except queue.Empty:
                raise socket.timeout()
            if chunk is None:
                self.eof[index] = True
            else:
                self.buffers[index].extend(chunk)
        data = bytes(self.buffers[index][:min(11, count)])
        del self.buffers[index][:len(data)]
        return data

    def recv(self, count):
        return self._receive(0, count)

    def recv_stderr(self, count):
        return self._receive(1, count)

    def send(self, data):
        if getattr(self.server, "drop_writes", False):
            return len(data)
        data = data[:9]
        return self.process.stdin.write(data)

    def exit_status_ready(self):
        return self.process.poll() is not None if self.process else super().exit_status_ready()

    def recv_exit_status(self):
        return self.process.wait() if self.process else super().recv_exit_status()

    def close(self):
        if self.closed:
            return
        if getattr(self, 'process', None) is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            for thread in self.pumps:
                thread.join(timeout=1)
        super().close()
