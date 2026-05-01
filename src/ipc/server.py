"""IPCServer — broker-side Unix-domain-socket publisher.

Async-native. Started from the broker's asyncio loop. Accepts at most one
trader connection at a time (v1 design — single trader peer). Events are
published via ``publish(msg)``: synchronous, non-blocking, drops on full
buffer (with policy depending on event type — kill/resume always go through).

Backpressure policy v1:
  - The asyncio StreamWriter has its own buffer (high-water mark).
  - When buffer is full, ``writer.write`` still succeeds but
    ``writer.drain()`` would block. We do NOT drain in the publish path,
    so the socket buffer can grow indefinitely if trader is too slow.
  - Phase 4 will replace this with bounded SHM rings that drop oldest
    non-critical events.

Failure modes:
  - Trader disconnects: caught, log, channel closes; future publishes
    no-op until reconnect.
  - Trader crashes mid-message: same as disconnect.
  - Socket file pre-existing: removed at startup (unlink on bind).
"""

import asyncio
import logging
import os

from .protocol import pack_frame

logger = logging.getLogger(__name__)


class IPCServer:
    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._server: asyncio.AbstractServer | None = None
        self._on_command = None  # optional: callable for trader → broker commands

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    def set_command_handler(self, handler) -> None:
        """Register a callable invoked with each decoded command from the
        trader. Signature: ``handler(msg: dict) -> None``."""
        self._on_command = handler

    async def start(self) -> None:
        # Remove stale socket file from a prior run.
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._socket_path
        )
        # Loose perms so the trader container (running as root in dev) can
        # connect; production should use a tighter umask + matching uid.
        try:
            os.chmod(self._socket_path, 0o660)
        except OSError as e:
            logger.warning("ipc: chmod failed on %s: %s", self._socket_path, e)
        logger.warning("IPC server listening on %s", self._socket_path)

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._writer is not None and not self._writer.is_closing():
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._writer is not None and not self._writer.is_closing():
            # v1: single trader peer. Reject second connection.
            logger.warning("ipc: rejecting second trader connection")
            writer.close()
            return
        peer = writer.get_extra_info("peername")
        logger.warning("IPC trader connected: %s", peer)
        self._writer = writer
        self._reader_task = asyncio.create_task(
            self._read_loop(reader), name="ipc-server-read"
        )

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        from .protocol import unpack_frames
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                for msg in unpack_frames(buf):
                    if not isinstance(msg, dict):
                        logger.debug("ipc: dropping non-dict frame from trader")
                        continue
                    if self._on_command is not None:
                        try:
                            self._on_command(msg)
                        except Exception:
                            logger.exception(
                                "ipc: command handler raised on %s",
                                msg.get("type", "?"),
                            )
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception as e:
            logger.warning("ipc: read loop error: %s", e)
        finally:
            logger.warning("IPC trader disconnected")
            try:
                self._writer.close() if self._writer else None
            except Exception:
                pass
            self._writer = None

    def publish(self, msg: dict) -> bool:
        """Publish an event to the trader. Returns True iff queued for send.

        Synchronous and non-blocking: writes to the asyncio StreamWriter's
        internal buffer. If trader isn't connected, drops the message.
        """
        w = self._writer
        if w is None or w.is_closing():
            return False
        try:
            w.write(pack_frame(msg))
        except Exception as e:
            logger.warning("ipc: publish failed: %s", e)
            return False
        return True
