"""Wire-protocol round-trip tests for src.ipc.protocol."""

import struct

import pytest

from src.ipc.protocol import pack_frame, unpack_frames


def test_round_trip_single_message():
    msg = {"type": "tick", "ts_ns": 1_700_000_000_000_000_000,
           "strike": 6.0, "bid": 0.12, "ask": 0.13}
    buf = bytearray(pack_frame(msg))
    out = list(unpack_frames(buf))
    assert out == [msg]
    assert len(buf) == 0  # all consumed


def test_round_trip_multiple_messages():
    msgs = [
        {"type": "tick", "ts_ns": 1, "strike": 6.0},
        {"type": "order_ack", "ts_ns": 2, "orderId": 12345},
        {"type": "fill", "ts_ns": 3, "qty": 1, "price": 0.1},
    ]
    buf = bytearray()
    for m in msgs:
        buf.extend(pack_frame(m))
    out = list(unpack_frames(buf))
    assert out == msgs
    assert len(buf) == 0


def test_partial_frame_left_in_buffer():
    msg = {"type": "tick", "ts_ns": 1, "strike": 6.0}
    full = pack_frame(msg)
    buf = bytearray(full[:-3])  # truncate last 3 bytes
    out = list(unpack_frames(buf))
    assert out == []  # nothing complete
    assert len(buf) == len(full) - 3  # buffer untouched


def test_oversized_length_raises():
    # Synthetic frame with absurd length prefix (>4 MiB cap)
    bogus = struct.pack(">I", 5 * 1024 * 1024)
    buf = bytearray(bogus)
    with pytest.raises(ValueError):
        list(unpack_frames(buf))


def test_malformed_msgpack_skipped():
    # Length says 4 bytes, body is non-msgpack garbage
    buf = bytearray(struct.pack(">I", 4) + b"\xff\xff\xff\xff")
    out = list(unpack_frames(buf))
    assert out == []  # malformed body silently skipped
    assert len(buf) == 0  # frame still consumed


def test_byte_types_preserved():
    msg = {"type": "snapshot", "ts_ns": 1, "blob": b"\x00\x01\x02"}
    buf = bytearray(pack_frame(msg))
    out = list(unpack_frames(buf))
    assert out == [msg]
    assert isinstance(out[0]["blob"], bytes)


def test_packed_frame_has_length_prefix():
    msg = {"type": "tick", "ts_ns": 1}
    frame = pack_frame(msg)
    declared = struct.unpack(">I", frame[:4])[0]
    assert len(frame) == 4 + declared
