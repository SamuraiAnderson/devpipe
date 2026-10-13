"""Short-lived, read-only TFTP distribution service (CORE-04).

TFTPy owns packet negotiation and block sequencing. This adapter owns the
listener, path policy, deadlines, logging and error propagation. In particular,
it does not use TFTPy's listener, which logs and swallows transfer failures.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import ExitStack
import contextvars
from dataclasses import dataclass
import math
import os
from pathlib import Path, PurePosixPath
import select
import socket
import struct
import threading
import time
from typing import Any
from types import TracebackType
import uuid

from ._logs import LogBuffer, SessionLogs
from .exceptions import RedPyMakeError, TransferError, UnsupportedOperationError


@dataclass
class _Transfer:
    context: Any
    filename: str
    source: Path
    request: bytes
    started: float
    last_activity: float
    retries: int = 0


class TftpServer:
    """A context-managed IPv4 TFTP download service; create with :func:`tftpd`.

    ``host``/``port`` expose the bound address after entry. ``wait(filename)``
    waits for the last ACK, while ``check()`` raises any background failure.
    The first failed request stops this short-lived service. All failures also
    surface on context exit. This is independent of Session and serial shells.
    """

    def __init__(self, root_dir: str | os.PathLike[str], *, host: str = "127.0.0.1",
                 port: int = 69, timeout: float = 2.0, retries: int = 3) -> None:
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a non-negative integer")
        self._root = Path(root_dir)
        self._host, self._port = host, port
        self._timeout, self._retries = timeout, retries
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._completed: set[str] = set()
        self._started = self._closed = False
        self._socket: socket.socket | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future[None] | None = None
        self._library: Any = None
        self._buffer = LogBuffer("tftp:" + uuid.uuid4().hex)
        self._logs = SessionLogs(self._buffer)

    @property
    def host(self) -> str:
        """IPv4 interface address, resolved on context entry."""
        return self._host

    @property
    def port(self) -> int:
        """Bound UDP port (including the chosen port when requested as zero)."""
        return self._port

    @property
    def root_dir(self) -> Path:
        """Local serving directory, resolved on context entry."""
        return self._root

    @property
    def logs(self) -> SessionLogs:
        """Service and transfer events, readable after close."""
        return self._logs

    def _event(self, event: str, message: str, *, level: str = "INFO", **fields: Any) -> None:
        self._buffer.append(event=event, level=level, stream="system", message=message,
                            fields=fields)

    def __enter__(self) -> TftpServer:
        if self._started or self._closed:
            raise UnsupportedOperationError("a TFTP context can only be entered once")
        try:
            import tftpy
        except ImportError as exc:
            raise UnsupportedOperationError(
                "TFTP support requires `pip install redpymake[tftp]`"
            ) from exc
        self._library = tftpy
        self._started = True
        try:
            self._root = self._root.resolve(strict=True)
            if not self._root.is_dir():
                raise NotADirectoryError(str(self._root))
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.bind((self._host, self._port))
            self._host, self._port = self._socket.getsockname()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpm-tftp")
            self._future = self._executor.submit(contextvars.copy_context().run, self._serve)
            self._future.add_done_callback(self._notify)
            self._event("tftp_open", f"TFTP ready at {self._host}:{self._port}", root=str(self._root))
        except Exception as exc:
            if self._socket is not None:
                self._socket.close()
            self._closed = True
            self._buffer.close()
            raise TransferError(f"cannot start TFTP at {self._host}:{self._port}: {exc}",
                                source=str(self._root), target=f"{self._host}:{self._port}",
                                cause=exc) from exc
        return self

    def _notify(self, future: Future[None]) -> None:
        with self._condition:
            self._condition.notify_all()

    def check(self) -> None:
        """Raise a background TransferError, or reject an inactive service."""
        if self._future is not None and self._future.done():
            self._future.result()  # Worker errors are always RedPyMakeError.
        if not self._started or self._closed:
            raise UnsupportedOperationError("TFTP service is not running")

    def wait(self, filename: str, timeout: float = 30.0) -> None:
        """Wait for one successful download of filename since context entry.

        A protocol error or timeout raises TransferError. Completion confirms
        the TFTP ACK, not a device-side flash, checksum or execution result.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("wait timeout must be finite and non-negative")
        expires = time.monotonic() + timeout
        with self._condition:
            while True:
                self.check()
                if filename in self._completed:
                    return
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise TransferError(f"timed out waiting for TFTP download of {filename!r}",
                                        source=str(self._root / filename))
                self._condition.wait(remaining)

    def close(self) -> None:
        """Stop active transfers, release resources and surface worker errors."""
        if self._closed:
            return
        self._stop.set()
        try:
            if self._future is not None:
                self._future.result(timeout=5)
        except FutureTimeout as exc:
            raise TransferError("TFTP worker did not stop within 5 seconds", cause=exc) from exc
        finally:
            self._closed = True
            finished = self._future is None or self._future.done()
            if self._executor is not None:
                self._executor.shutdown(wait=finished, cancel_futures=True)
            if finished:
                self._event("tftp_closed", "TFTP service closed")
                self._buffer.close()
            with self._condition:
                self._condition.notify_all()

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> bool:
        try:
            self.close()
        except RedPyMakeError as service_error:
            if exc is not None and exc is not service_error:
                raise exc.with_traceback(tb) from service_error
            raise
        return False

    def _source(self, filename: str) -> Path:
        parts = PurePosixPath(filename)
        if (not filename or parts.is_absolute() or ".." in parts.parts
                or "\\" in filename or ":" in filename):
            raise PermissionError(f"TFTP requires a relative path inside root: {filename!r}")
        source = (self._root / filename).resolve(strict=True)
        if not source.is_relative_to(self._root):
            raise PermissionError(f"TFTP path escapes root: {filename!r}")
        if not source.is_file():
            raise PermissionError(f"TFTP only serves regular files: {filename!r}")
        return source

    def _start(self, packet: bytes, peer: tuple[str, int], active: dict) -> None:
        assert self._socket is not None
        filename = "<invalid request>"
        try:
            request = self._library.TftpPacketFactory.TftpPacketFactory().parse(packet)
            filename = getattr(request, "filename", filename)
            if not isinstance(request, self._library.TftpPacketTypes.TftpPacketRRQ):
                raise PermissionError("TFTP service is read-only; only RRQ downloads are supported")
            if request.mode != "octet":
                raise ValueError("only octet mode is supported")
            source = self._source(filename)
            if peer in active:
                transfer = active[peer]
                if packet != transfer.request:
                    raise ValueError("client changed its request during a transfer")
                self._retry(transfer, peer)
                return
            context = self._library.TftpContexts.TftpContextServer(
                peer[0], peer[1], self._timeout, str(self._root), flock=False,
            )
            now = time.monotonic()
            active[peer] = _Transfer(context, filename, source, packet, now, now)
            context.sock.bind((self._host, 0))
            context.start(packet)
            self._event("transfer_start", f"TFTP download {filename}", filename=filename, peer=peer)
        except Exception as exc:
            code = 1 if isinstance(exc, FileNotFoundError) else 2 if isinstance(exc, PermissionError) else 4
            message = {1: b"File not found", 2: b"Access violation", 4: b"Illegal TFTP operation"}[code]
            self._socket.sendto(struct.pack("!HH", 5, code) + message + b"\0", peer)
            raise TransferError(f"TFTP request {filename!r} from {peer}: {exc}",
                                source=filename, target=str(peer), cause=exc) from exc

    def _retry(self, transfer: _Transfer, peer: tuple[str, int]) -> None:
        if transfer.retries >= self._retries:
            cause = TimeoutError("waiting for TFTP ACK")
            raise TransferError(f"TFTP transfer timed out: {transfer.filename!r}",
                                source=str(transfer.source), target=str(peer), cause=cause) from cause
        transfer.retries += 1
        self._event("transfer_retry", f"TFTP retransmit {transfer.filename}", level="WARNING",
                    filename=transfer.filename, attempt=transfer.retries, peer=peer)
        transfer.context.state.resendLast()
        transfer.last_activity = time.monotonic()

    def _serve(self) -> None:
        assert self._socket is not None
        active: dict[tuple[str, int], _Transfer] = {}
        try:
            try:
                while not self._stop.is_set():
                    sockets = {t.context.sock: peer for peer, t in active.items()}
                    readable, _, _ = select.select([self._socket, *sockets], [], [], min(.05, self._timeout))
                    for sock in readable:
                        if sock is self._socket:
                            packet, peer = sock.recvfrom(65535)
                            self._start(packet, peer, active)
                            continue
                        peer = sockets[sock]
                        transfer = active[peer]
                        try:
                            transfer.context.cycle()
                        except self._library.TftpTimeout:
                            # A protocol-requested retransmission, bounded and logged.
                            self._retry(transfer, peer)
                            continue
                        transfer.last_activity = time.monotonic()
                        transfer.retries = 0
                        if transfer.context.state is None:
                            transfer.context.end()
                            del active[peer]
                            self._event("transfer_end", f"TFTP download complete: {transfer.filename}",
                                filename=transfer.filename, peer=peer,
                                bytes_transferred=transfer.context.metrics.bytes,
                                duration=time.monotonic() - transfer.started)
                            with self._condition:
                                self._completed.add(transfer.filename)
                                self._condition.notify_all()
                    now = time.monotonic()
                    for peer, transfer in active.items():
                        if now - transfer.last_activity >= self._timeout:
                            self._retry(transfer, peer)
            finally:
                # ExitStack runs all cleanup callbacks even if one cleanup fails.
                with ExitStack() as cleanup:
                    cleanup.callback(self._socket.close)
                    for transfer in active.values():
                        cleanup.callback(transfer.context.end)
        except Exception as exc:
            self._event("transfer_error", str(exc), level="ERROR")
            if isinstance(exc, TransferError):
                raise
            raise TransferError(f"TFTP service failed: {exc}", source=str(self._root),
                                target=f"{self._host}:{self._port}", cause=exc) from exc


def tftpd(root_dir: str | os.PathLike[str], *, host: str = "127.0.0.1", port: int = 69,
          timeout: float = 2.0, retries: int = 3) -> TftpServer:
    """Serve local files read-only inside ``with``; requires ``redpymake[tftp]``.

    Bind ``host`` to the interface reachable by the device. ``port=0`` selects
    a free port for tests. ``timeout`` and ``retries`` bound per-block ACK waits.
    Use the returned server's ``wait(filename)`` to confirm a download.
    """
    return TftpServer(root_dir, host=host, port=port, timeout=timeout, retries=retries)
