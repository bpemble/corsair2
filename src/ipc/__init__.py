"""IPC wire-format spec — kept as parity reference after the Rust
broker took over the IPC server/client roles in Phase 6.7.

Only `protocol.py` survives; server.py/client.py/shm.py were removed
along with the Python broker. The Rust-side wire format MUST match
what `protocol.py` defines — `tests/test_ipc_protocol.py` enforces
this against round-trips of pack_frame / unpack_frames.
"""
from .protocol import (
    EVENT_TYPES,
    COMMAND_TYPES,
    pack_frame,
    unpack_frames,
    DEFAULT_SOCKET_PATH,
)

__all__ = [
    "EVENT_TYPES",
    "COMMAND_TYPES",
    "pack_frame",
    "unpack_frames",
    "DEFAULT_SOCKET_PATH",
]
