"""IPCClient — trader-side Unix-domain-socket consumer.

Connects to the broker's socket, reads framed events, hands them to a
caller-supplied async callback. Sends commands back via ``send()``.

Reconnect strategy: on broker disconnect, sleep + retry forever (with
exponential backoff capped at 5s). Trader main loop must tolerate the
"no broker" gap.
"""

import asyncio
import logging
from typing import Awaitable, Callable

from .protocol import pack_frame, unpack_frames

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


class IPCClient:
    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def run(self, on_event: EventHandler) -> None:
        """Connect-loop forever. Calls ``on_event`` for every received frame.

        Returns only when cancelled. Exceptions raised by ``on_event`` are
        logged and swallowed — one bad event must not crash the trader's
        IPC loop.
        """
        backoff = 0.1
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    self._socket_path
                )
                logger.warning("IPC client connected to %s", self._socket_path)
                self._connected.set()
                backoff = 0.1  # reset
                await self._read_loop(on_event)
            except (FileNotFoundError, ConnectionRefusedError) as e:
                logger.info("ipc: broker not ready (%s); retry in %.1fs",
                            type(e).__name__, backoff)
            except (ConnectionError, asyncio.IncompleteReadError) as e:
                logger.warning("ipc: connection lost (%s); reconnecting", e)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ipc: unexpected error; reconnecting")
            finally:
                self._connected.clear()
                if self._writer is not None and not self._writer.is_closing():
                    try:
                        self._writer.close()
                        await self._writer.wait_closed()
                    except Exception:
                        pass
                self._reader = self._writer = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def _read_loop(self, on_event: EventHandler) -> None:
        assert self._reader is not None
        buf = bytearray()
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionError("broker closed connection")
            buf.extend(chunk)
            for msg in unpack_frames(buf):
                if not isinstance(msg, dict):
                    continue
                try:
                    await on_event(msg)
                except Exception:
                    logger.exception(
                        "ipc: event handler raised on %s",
                        msg.get("type", "?"),
                    )

    def send(self, msg: dict) -> bool:
        """Send a command to the broker. Non-blocking; returns True iff queued."""
        w = self._writer
        if w is None or w.is_closing():
            return False
        try:
            w.write(pack_frame(msg))
        except Exception as e:
            logger.warning("ipc: send failed: %s", e)
            return False
        return True
