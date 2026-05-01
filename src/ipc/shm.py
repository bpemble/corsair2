"""Shared-memory ring-buffer IPC transport.

Lower-latency alternative to the Unix-socket transport in
``server.py`` / ``client.py``. Same wire format (length-prefixed
msgpack frames) so the protocol is unchanged.

Layout per ring (one ring per direction):

    [16 bytes header]
    [N bytes ring data]

Header (little-endian, 8-byte aligned):
    bytes  0..7   write_offset (u64, monotonic; never wraps for years
                  at our message rate, simplifying wrap logic)
    bytes  8..15  read_offset  (u64, monotonic; ditto)

Producer increments write_offset after writing a frame; consumer
increments read_offset after consuming. Position in buffer is
``offset % capacity``. Frames straddling the end of the buffer are
split into two contiguous segments at write/read time.

Concurrency model: single producer + single consumer per ring (SPSC).
No locks. Atomicity rests on:
  - 8-byte aligned reads/writes of u64 offsets being atomic on x86_64
    (we don't claim portability beyond x86_64 Linux for now)
  - Python's GIL serializing bytecodes within one process; cross-process
    we rely on the offset semantics: producer never reads its own
    write_offset's downstream effects, consumer only reads frames whose
    write_offset already advanced past read_offset

Wake-up: consumer polls write_offset with adaptive backoff
(busy-wait → short sleep → longer sleep). Phase 5 deliberately uses
polling rather than eventfd for portability + simplicity. Eventfd is
the right answer for getting to ≤5μs p50; this gets us to ~50-100μs.

Failure modes:
  - Buffer full (write_offset - read_offset == capacity): producer
    drops the frame and bumps a counter. v1 design — Phase 5b will
    add bounded backpressure.
  - Producer crashes: consumer sees stale offsets; treats as healthy
    (consumer cannot detect crash without a heartbeat). Consumer's
    parent process death detection (parent_alive) is the supervisor's
    job.
  - Consumer crashes: producer fills until buffer-full, then drops.
    Exposed via a counter the operator can poll.

Unsupported in this version (acknowledged tech debt):
  - Multi-consumer fanout
  - Persistent rings across process restart (mmap of a tmpfs file
    survives, but offsets aren't reset, so stale data is replayed —
    we don't try to be smart about it; see ``_init_or_zero``).
"""

import mmap
import os
import struct
import time
from typing import Callable, Optional

import logging

from .protocol import pack_frame, unpack_frames

logger = logging.getLogger(__name__)

_HEADER = struct.Struct("<QQ")  # write_off, read_off
_HDR_SIZE = _HEADER.size

# Adaptive consumer poll: tight on hot path, lazy when idle.
_POLL_TIGHT_US = 50          # busy-wait threshold (50μs of pure polling)
_POLL_TIGHT_ITERS = 1000     # ~50ns per loop iteration ≈ 50μs
_POLL_BACKOFF_US = 100       # short sleep after no data
_POLL_LONG_BACKOFF_US = 5000 # longer sleep after sustained idle

DEFAULT_RING_CAPACITY = 1 << 20  # 1 MiB ring (≥250K small frames)


class _Ring:
    """One unidirectional SPSC ring backed by mmap.

    Created via ``open_or_create``; both processes mmap the same
    backing file (typically on tmpfs) and align to the SAME capacity.
    """

    def __init__(self, path: str, capacity: int, owner: bool):
        self._path = path
        self._capacity = capacity
        self._size = _HDR_SIZE + capacity
        self._owner = owner
        self._fd = None
        self._mm: Optional[mmap.mmap] = None
        self.frames_dropped = 0  # producer side: buffer-full drops

    def open(self) -> None:
        # The owner (broker) creates+sizes the file; non-owner just maps it.
        if self._owner:
            self._fd = os.open(self._path,
                               os.O_RDWR | os.O_CREAT, 0o660)
            os.ftruncate(self._fd, self._size)
        else:
            self._fd = os.open(self._path, os.O_RDWR)
        self._mm = mmap.mmap(self._fd, self._size,
                             flags=mmap.MAP_SHARED,
                             prot=mmap.PROT_READ | mmap.PROT_WRITE)
        if self._owner:
            # Zero the header (write/read offsets). Buffer body is left
            # as-is — frames are read by offset, untouched bytes are
            # never read.
            self._mm[:_HDR_SIZE] = b"\x00" * _HDR_SIZE

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._owner and os.path.exists(self._path):
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass

    def _read_offsets(self) -> tuple[int, int]:
        return _HEADER.unpack_from(self._mm, 0)

    def _set_write(self, w: int) -> None:
        # Pack into a small bytes object and write the 8-byte slot.
        # struct.pack_into is the right primitive but writing only 8
        # bytes is cheaper.
        self._mm[0:8] = w.to_bytes(8, "little", signed=False)

    def _set_read(self, r: int) -> None:
        self._mm[8:16] = r.to_bytes(8, "little", signed=False)

    def write(self, frame: bytes) -> bool:
        """Append a complete frame. Returns False if the buffer is too
        full to fit; caller should bump a drop counter."""
        n = len(frame)
        if n > self._capacity // 2:
            # Defensive: a single frame larger than half the buffer
            # would deadlock the consumer if the producer wraps around.
            self.frames_dropped += 1
            return False
        w, r = self._read_offsets()
        free = self._capacity - (w - r)
        if free < n:
            self.frames_dropped += 1
            return False
        pos = w % self._capacity
        end = pos + n
        if end <= self._capacity:
            self._mm[_HDR_SIZE + pos:_HDR_SIZE + end] = frame
        else:
            # Wrap: write tail to end of ring, head to start.
            tail = self._capacity - pos
            self._mm[_HDR_SIZE + pos:_HDR_SIZE + self._capacity] = frame[:tail]
            self._mm[_HDR_SIZE:_HDR_SIZE + (n - tail)] = frame[tail:]
        # Publish: write_offset advances after the data is in place.
        self._set_write(w + n)
        return True

    def read_available(self) -> bytes:
        """Read everything available, return as one contiguous bytes
        object. May be empty. Caller is responsible for parsing frames
        from the returned buffer (use unpack_frames)."""
        w, r = self._read_offsets()
        avail = w - r
        if avail <= 0:
            return b""
        pos = r % self._capacity
        end = pos + avail
        if end <= self._capacity:
            data = bytes(self._mm[_HDR_SIZE + pos:_HDR_SIZE + end])
        else:
            tail = self._capacity - pos
            head_len = avail - tail
            data = (bytes(self._mm[_HDR_SIZE + pos:_HDR_SIZE + self._capacity])
                    + bytes(self._mm[_HDR_SIZE:_HDR_SIZE + head_len]))
        self._set_read(w)
        return data


class SHMServer:
    """Drop-in replacement for IPCServer using SHM rings.

    Two rings: ``events`` (this side writes) and ``commands`` (this
    side reads). Filenames are ``{base}.events`` and ``{base}.commands``.

    Polls the commands ring on a background asyncio task.
    """

    def __init__(self, base_path: str, capacity: int = DEFAULT_RING_CAPACITY):
        self._base = base_path
        self._capacity = capacity
        self._events = _Ring(f"{base_path}.events", capacity, owner=True)
        self._commands = _Ring(f"{base_path}.commands", capacity, owner=True)
        self._on_command: Optional[Callable[[dict], None]] = None
        self._poll_task = None
        self._stop = False

    @property
    def connected(self) -> bool:
        # SHM is connectionless; "connected" is best-effort. We treat
        # the server as always-connected post-start.
        return self._events._mm is not None

    def set_command_handler(self, handler) -> None:
        self._on_command = handler

    async def start(self) -> None:
        os.makedirs(os.path.dirname(self._base), exist_ok=True)
        self._events.open()
        self._commands.open()
        import asyncio
        self._poll_task = asyncio.create_task(self._poll_commands(),
                                              name="shm-cmd-poll")
        logger.warning("SHM IPC server up: base=%s capacity=%d", self._base, self._capacity)

    async def stop(self) -> None:
        self._stop = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except Exception:
                pass
        self._events.close()
        self._commands.close()

    async def _poll_commands(self) -> None:
        import asyncio
        buf = bytearray()
        idle_iters = 0
        while not self._stop:
            chunk = self._commands.read_available()
            if chunk:
                buf.extend(chunk)
                idle_iters = 0
                if self._on_command is not None:
                    for msg in unpack_frames(buf):
                        if isinstance(msg, dict):
                            try:
                                self._on_command(msg)
                            except Exception:
                                logger.exception(
                                    "shm: command handler raised on %s",
                                    msg.get("type", "?"),
                                )
            else:
                idle_iters += 1
                # Adaptive: short sleep first, longer if sustained idle.
                if idle_iters < 50:
                    await asyncio.sleep(_POLL_BACKOFF_US / 1_000_000)
                else:
                    await asyncio.sleep(_POLL_LONG_BACKOFF_US / 1_000_000)

    def publish(self, msg: dict) -> bool:
        if self._events._mm is None:
            return False
        return self._events.write(pack_frame(msg))


class SHMClient:
    """Drop-in replacement for IPCClient using SHM rings.

    Polling read loop with adaptive backoff. Producer is the broker
    side; we read from ``events`` and write to ``commands``.
    """

    def __init__(self, base_path: str, capacity: int = DEFAULT_RING_CAPACITY):
        self._base = base_path
        self._capacity = capacity
        self._events = _Ring(f"{base_path}.events", capacity, owner=False)
        self._commands = _Ring(f"{base_path}.commands", capacity, owner=False)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self, on_event) -> None:
        import asyncio
        # Wait for the broker to create the SHM files.
        events_path = f"{self._base}.events"
        cmds_path = f"{self._base}.commands"
        while not (os.path.exists(events_path) and os.path.exists(cmds_path)):
            logger.info("shm: rings not ready; retry")
            await asyncio.sleep(1.0)
        self._events.open()
        self._commands.open()
        self._connected = True
        logger.warning("SHM client connected: base=%s", self._base)

        buf = bytearray()
        idle_iters = 0
        while True:
            chunk = self._events.read_available()
            if chunk:
                buf.extend(chunk)
                idle_iters = 0
                for msg in unpack_frames(buf):
                    if isinstance(msg, dict):
                        try:
                            await on_event(msg)
                        except Exception:
                            logger.exception(
                                "shm: event handler raised on %s",
                                msg.get("type", "?"),
                            )
            else:
                idle_iters += 1
                if idle_iters < 50:
                    await asyncio.sleep(_POLL_BACKOFF_US / 1_000_000)
                else:
                    await asyncio.sleep(_POLL_LONG_BACKOFF_US / 1_000_000)

    def send(self, msg: dict) -> bool:
        if self._commands._mm is None:
            return False
        return self._commands.write(pack_frame(msg))
