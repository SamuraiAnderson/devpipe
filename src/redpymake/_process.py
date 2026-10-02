"""Binary process IO shared by SSH command capture and device transports."""
from __future__ import annotations

import codecs
import contextvars
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .exceptions import CommandTimeoutError, SessionConnectionError


@dataclass(frozen=True)
class _Deadline:
    timeout: float | None
    expires: float | None

    @classmethod
    def start(cls, timeout: float | None) -> _Deadline:
        return cls(timeout, None if timeout is None else time.monotonic() + max(0, timeout))

    def remaining(self) -> float | None:
        if self.expires is None:
            return None
        left = self.expires - time.monotonic()
        if left <= 0:
            raise CommandTimeoutError("operation deadline exceeded", timeout=self.timeout)
        return left


class _SshProcess:
    """A channel is a binary stream. No PTY, shell dialogue, or text wrappers."""

    def __init__(self, channel: Any, owner: Any) -> None:
        self.channel = channel
        self.owner = owner
        self._closed = False
        self._write_lock = threading.Lock()

    def read(self, count: int, *, stderr: bool = False) -> bytes:
        return (self.channel.recv_stderr if stderr else self.channel.recv)(count)

    def write(self, data: bytes, deadline: _Deadline) -> None:
        with self._write_lock:
            offset = 0
            while offset < len(data):
                self.owner._check_alive()
                deadline.remaining()
                try:
                    sent = self.channel.send(data[offset:offset + 32768])
                except socket.timeout:
                    continue  # A polling timeout is not EOF or a retry of sent bytes.
                if sent <= 0:
                    raise OSError("SSH channel closed during write; delivery is unknown")
                offset += sent

    def poll(self) -> int | None:
        if self.channel.exit_status_ready():
            return self.channel.recv_exit_status()
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.channel.close()
        finally:
            self.owner._release_process(self)


def _capture(process: _SshProcess, *, session: Any, command: Sequence[str],
             timeout: float | None, encoding: str = "utf-8",
             operation_id: str | None = None, line_filter: Any = None,
             deadline: _Deadline | None = None) -> tuple[int, str, str]:
    """Drain both streams through EOF before closing, including exit-ready tails."""
    deadline = deadline or _Deadline.start(timeout)
    chunks: list[list[bytes]] = [[], []]
    errors: list[Exception] = []
    done = [threading.Event(), threading.Event()]

    def pump(index: int) -> None:
        pending = ""

        def emit(text: str, final: bool = False) -> None:
            nonlocal pending
            if operation_id is None:
                return
            pending += text
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                if line_filter is not None and not line_filter(index, line.rstrip("\r")):
                    continue
                session._log_buffer.append(event="command_output", level="INFO",
                    stream="stderr" if index else "stdout", message=line.rstrip("\r"),
                    operation_id=operation_id)
            if final and pending:
                if line_filter is not None and not line_filter(index, pending.rstrip("\r")):
                    return
                session._log_buffer.append(event="command_output", level="INFO",
                    stream="stderr" if index else "stdout", message=pending.rstrip("\r"),
                    operation_id=operation_id)

        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            while True:
                session._check_alive()
                try:
                    data = process.read(32768, stderr=bool(index))
                except socket.timeout:
                    if process._closed:
                        break
                    continue
                if not data:
                    break
                chunks[index].append(data)
                emit(decoder.decode(data))
            emit(decoder.decode(b"", final=True), final=True)
        except Exception as exc:
            errors.append(exc)  # Main thread immediately raises; never treated as EOF.
        finally:
            done[index].set()

    threads = []
    for index in (0, 1):
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(pump, index), daemon=True,
                                  name=f"rpm-ssh-{'err' if index else 'out'}")
        thread.start()
        threads.append(thread)

    def output(index: int) -> str:
        return b"".join(chunks[index]).decode(encoding, errors="replace").rstrip("\r\n")

    primary: BaseException | None = None
    try:
        while True:
            session._check_alive()
            if errors:
                raise SessionConnectionError(
                    f"{session.label}: output read failed: {errors[0]}; result unknown",
                    session=session, cause=errors[0], stdout=output(0), stderr=output(1),
                    outcome="unknown") from errors[0]
            deadline.remaining()
            rc = process.poll()
            if rc is not None and all(event.is_set() for event in done):
                if errors:
                    continue  # A pump can fail between the checks above and EOF.
                if rc < 0:
                    raise SessionConnectionError("SSH closed without an exit status; result unknown",
                        session=session, stdout=output(0), stderr=output(1), outcome="unknown")
                return rc, output(0), output(1)
            time.sleep(0.005)
    except (TimeoutError, CommandTimeoutError) as exc:
        primary = exc
        raise CommandTimeoutError("command deadline exceeded; remote completion is unconfirmed",
            session=session, command=command, timeout=timeout,
            stdout=output(0), stderr=output(1)) from exc
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            process.close()
        except Exception as exc:
            if primary is None:
                raise SessionConnectionError(f"failed to close SSH command channel: {exc}",
                                             session=session, cause=exc) from exc
            setattr(primary, "cleanup_error", exc)
        finally:
            for thread in threads:
                thread.join(timeout=1)
