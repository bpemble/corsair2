"""IPC layer between broker (current corsair process) and trader.

Two transports available:
  - "socket" (default): asyncio Unix domain socket. ~330μs p50 latency,
    reliable, simple to debug. ``server.py`` / ``client.py``.
  - "shm": memory-mapped ring buffers, polling consumer. ~50-100μs p50.
    Phase 5 of mm_service_split. ``shm.py``.

Pick via the ``CORSAIR_IPC_TRANSPORT`` env var (read at process start).
Both broker and trader must agree — if they disagree, the trader will
loop trying to connect.

Wire format is the same across transports: length-prefixed msgpack
frames. See ``protocol.py`` for message types.

Default paths:
  - socket: ``/app/data/corsair_ipc.sock``
  - shm:    ``/app/data/corsair_ipc`` (.events / .commands suffixed)
"""

import os

from .protocol import (
    EVENT_TYPES,
    COMMAND_TYPES,
    pack_frame,
    unpack_frames,
    DEFAULT_SOCKET_PATH,
)
from .server import IPCServer
from .client import IPCClient
from .shm import SHMServer, SHMClient


DEFAULT_SHM_BASE = "/app/data/corsair_ipc"


def _transport() -> str:
    return os.environ.get("CORSAIR_IPC_TRANSPORT", "socket").strip().lower()


def make_server():
    """Return the Server instance for the configured transport."""
    if _transport() == "shm":
        return SHMServer(DEFAULT_SHM_BASE)
    return IPCServer(DEFAULT_SOCKET_PATH)


def make_client():
    """Return the Client instance for the configured transport."""
    if _transport() == "shm":
        return SHMClient(DEFAULT_SHM_BASE)
    return IPCClient(DEFAULT_SOCKET_PATH)


__all__ = [
    "EVENT_TYPES",
    "COMMAND_TYPES",
    "pack_frame",
    "unpack_frames",
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_SHM_BASE",
    "IPCServer",
    "IPCClient",
    "SHMServer",
    "SHMClient",
    "make_server",
    "make_client",
]
