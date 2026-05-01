//! Wire protocol: length-prefixed msgpack frames, identical to
//! src/ipc/protocol.py. The framing format is:
//!
//!     [4-byte big-endian uint32 length][msgpack body]
//!
//! Body is a msgpack map whose "type" key identifies the message.

use std::io::{self, Read, Write};

pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;

/// Pack a serialized msgpack body into a length-prefixed frame.
pub fn pack_frame(body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 + body.len());
    out.extend_from_slice(&(body.len() as u32).to_be_bytes());
    out.extend_from_slice(body);
    out
}

/// Try to unpack one complete frame from the front of `buf`. If a
/// complete frame is available, returns `Ok(Some(body))` and advances
/// `buf`. If incomplete, returns `Ok(None)` (caller waits for more
/// data). On a malformed length header, returns `Err`.
pub fn try_unpack_frame(buf: &mut Vec<u8>) -> io::Result<Option<Vec<u8>>> {
    if buf.len() < 4 {
        return Ok(None);
    }
    let mut len_bytes = [0u8; 4];
    len_bytes.copy_from_slice(&buf[..4]);
    let size = u32::from_be_bytes(len_bytes) as usize;
    if size > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("oversize frame: {} > {}", size, MAX_FRAME_BYTES),
        ));
    }
    if buf.len() < 4 + size {
        return Ok(None);
    }
    let body = buf[4..4 + size].to_vec();
    buf.drain(..4 + size);
    Ok(Some(body))
}

/// Unpack ALL complete frames in front of buf, leaving partial frame
/// at tail. Returns the list of msgpack body buffers.
pub fn unpack_all_frames(buf: &mut Vec<u8>) -> io::Result<Vec<Vec<u8>>> {
    let mut out = Vec::new();
    loop {
        match try_unpack_frame(buf)? {
            Some(body) => out.push(body),
            None => break,
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_one_frame() {
        let body = b"hello";
        let frame = pack_frame(body);
        let mut buf = frame.clone();
        let decoded = try_unpack_frame(&mut buf).unwrap().unwrap();
        assert_eq!(decoded, body);
        assert!(buf.is_empty());
    }

    #[test]
    fn partial_frame_returns_none() {
        let mut buf = vec![0, 0, 0, 5, b'h']; // len=5 but only 1 body byte
        let r = try_unpack_frame(&mut buf).unwrap();
        assert!(r.is_none());
        assert_eq!(buf.len(), 5); // untouched
    }

    #[test]
    fn unpack_all_returns_multiple() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&pack_frame(b"a"));
        buf.extend_from_slice(&pack_frame(b"bb"));
        buf.extend_from_slice(&pack_frame(b"ccc"));
        let frames = unpack_all_frames(&mut buf).unwrap();
        assert_eq!(frames.len(), 3);
        assert_eq!(frames[0], b"a");
        assert_eq!(frames[2], b"ccc");
        assert!(buf.is_empty());
    }
}
