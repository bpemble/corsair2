"""IPC layer between broker (current corsair process) and trader.

The broker owns the gateway connection (ib_insync clientId=0) and forwards
gateway events to the trader over a Unix domain socket. The trader sends
order-placement requests (and telemetry) back over the same socket.

V1 wire format: length-prefixed msgpack frames. See ``protocol.py`` for
message types. V2 (Phase 4+) will swap to shared-memory ring buffers
behind the same Server/Client abstractions.

Path: ``/app/data/corsair_ipc.sock`` — inside the shared data volume so
both containers see it. Brokers writes the socket; trader connects to it.
"""

from .protocol import (
    EVENT_TYPES,
    COMMAND_TYPES,
    pack_frame,
    unpack_frames,
    DEFAULT_SOCKET_PATH,
)
from .server import IPCServer
from .client import IPCClient

__all__ = [
    "EVENT_TYPES",
    "COMMAND_TYPES",
    "pack_frame",
    "unpack_frames",
    "DEFAULT_SOCKET_PATH",
    "IPCServer",
    "IPCClient",
]
