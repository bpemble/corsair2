//! SHM ring + FIFO notification client. Mirror of Python's
//! `src/ipc/shm.py` SHMClient. Uses memmap2 for the ring, native
//! Unix syscalls for the FIFO. Tokio-friendly: blocking reads on
//! the FIFO are wrapped in tokio::task::spawn_blocking.
//!
//! Layout per ring (must match Python):
//!     [16 bytes header][N bytes ring data]
//! Header (little-endian):
//!     bytes  0..7   write_offset (u64, monotonic)
//!     bytes  8..15  read_offset  (u64, monotonic)

use memmap2::{MmapMut, MmapOptions};
use std::fs::OpenOptions;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::RawFd;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

const HDR_SIZE: usize = 16;
pub const DEFAULT_RING_CAPACITY: usize = 1 << 20; // 1 MiB

/// One unidirectional SPSC ring backed by mmap. The trader uses two:
/// `events` (read from broker) and `commands` (write to broker).
pub struct Ring {
    mmap: MmapMut,
    capacity: usize,
    notify_w_fd: Option<RawFd>,
    notify_r_fd: Option<RawFd>,
    pub frames_dropped: u64,
}

impl Ring {
    /// Open an existing ring file (broker is the owner; trader maps
    /// the same backing file). Caller passes the path WITHOUT the
    /// `.notify` suffix — that's appended internally for the FIFO.
    pub fn open(path: &Path, capacity: usize) -> io::Result<Self> {
        let total = HDR_SIZE + capacity;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)?;
        let mmap = unsafe { MmapOptions::new().len(total).map_mut(&file)? };
        Ok(Self {
            mmap,
            capacity,
            notify_w_fd: None,
            notify_r_fd: None,
            frames_dropped: 0,
        })
    }

    /// Open the notification FIFO (`<ring>.notify`) in the requested
    /// direction. Same flags as Python: O_RDWR | O_NONBLOCK.
    pub fn open_notify(&mut self, path: &Path, as_writer: bool) -> io::Result<()> {
        // O_RDWR avoids the producer-blocks-on-no-reader problem at open time.
        let fd = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)?
            .into_raw_fd_keep();
        if as_writer {
            self.notify_w_fd = Some(fd);
        } else {
            self.notify_r_fd = Some(fd);
        }
        Ok(())
    }

    pub fn notify_r_fd(&self) -> Option<RawFd> {
        self.notify_r_fd
    }

    /// Producer side: signal the consumer that data is available.
    pub fn notify(&self) {
        if let Some(fd) = self.notify_w_fd {
            // SAFETY: best-effort 1-byte write; ignore errors (FIFO
            // buffer full or closed).
            let buf = [0u8; 1];
            unsafe {
                libc::write(fd, buf.as_ptr() as *const _, 1);
            }
        }
    }

    /// Consumer side: drain any pending notification bytes. Coalesces
    /// many producer writes into one wake-up.
    pub fn drain_notify(&self) {
        if let Some(fd) = self.notify_r_fd {
            let mut buf = [0u8; 4096];
            unsafe {
                // Read until EAGAIN (fd is non-blocking).
                loop {
                    let n = libc::read(fd, buf.as_mut_ptr() as *mut _, buf.len());
                    if n <= 0 {
                        break;
                    }
                }
            }
        }
    }

    fn read_offsets(&self) -> (u64, u64) {
        let bytes = &self.mmap[..HDR_SIZE];
        let w = u64::from_le_bytes(bytes[0..8].try_into().unwrap());
        let r = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
        (w, r)
    }

    fn set_write(&mut self, w: u64) {
        self.mmap[0..8].copy_from_slice(&w.to_le_bytes());
    }

    fn set_read(&mut self, r: u64) {
        self.mmap[8..16].copy_from_slice(&r.to_le_bytes());
    }

    /// Write a complete frame. Returns false if the buffer can't fit
    /// it; caller should retry or drop.
    pub fn write_frame(&mut self, frame: &[u8]) -> bool {
        let n = frame.len();
        if n > self.capacity / 2 {
            self.frames_dropped += 1;
            return false;
        }
        let (w, r) = self.read_offsets();
        let free = self.capacity as u64 - (w - r);
        if (free as usize) < n {
            self.frames_dropped += 1;
            return false;
        }
        let pos = (w as usize) % self.capacity;
        let end = pos + n;
        if end <= self.capacity {
            self.mmap[HDR_SIZE + pos..HDR_SIZE + end].copy_from_slice(frame);
        } else {
            let tail = self.capacity - pos;
            self.mmap[HDR_SIZE + pos..HDR_SIZE + self.capacity]
                .copy_from_slice(&frame[..tail]);
            self.mmap[HDR_SIZE..HDR_SIZE + (n - tail)]
                .copy_from_slice(&frame[tail..]);
        }
        // Publish: write_offset advances after data is in place.
        // We need a release-ordered store so the body bytes are visible
        // before the offset bump. Use atomic_store via the raw pointer.
        let new_w = w + n as u64;
        unsafe {
            let ptr = self.mmap.as_mut_ptr() as *mut AtomicU64;
            (*ptr).store(new_w, Ordering::Release);
        }
        // Notify consumer.
        self.notify();
        true
    }

    /// Consumer side: read everything available. Returns the bytes;
    /// caller is responsible for parsing frames out of them.
    pub fn read_available(&mut self) -> Vec<u8> {
        let (w, r) = self.read_offsets();
        let avail = w.saturating_sub(r) as usize;
        if avail == 0 {
            return Vec::new();
        }
        let pos = (r as usize) % self.capacity;
        let end = pos + avail;
        let mut data = Vec::with_capacity(avail);
        if end <= self.capacity {
            data.extend_from_slice(&self.mmap[HDR_SIZE + pos..HDR_SIZE + end]);
        } else {
            let tail = self.capacity - pos;
            let head_len = avail - tail;
            data.extend_from_slice(&self.mmap[HDR_SIZE + pos..HDR_SIZE + self.capacity]);
            data.extend_from_slice(&self.mmap[HDR_SIZE..HDR_SIZE + head_len]);
        }
        // Bump read offset.
        self.set_read(r + avail as u64);
        data
    }
}

impl Drop for Ring {
    fn drop(&mut self) {
        for fd in [self.notify_w_fd.take(), self.notify_r_fd.take()] {
            if let Some(fd) = fd {
                unsafe {
                    libc::close(fd);
                }
            }
        }
    }
}

// Local helper trait — std doesn't expose into_raw_fd that keeps the
// fd open after File drop. We need that because we want the fd to
// outlive the File handle.
trait IntoRawFdKeep {
    fn into_raw_fd_keep(self) -> RawFd;
}

impl IntoRawFdKeep for std::fs::File {
    fn into_raw_fd_keep(self) -> RawFd {
        let fd = self.as_raw_fd();
        // Forget the File so its Drop doesn't close the fd.
        std::mem::forget(self);
        fd
    }
}

/// Wait for the broker to create the SHM ring files at the given base
/// path. Returns once both `<base>.events` and `<base>.commands` exist.
pub async fn wait_for_rings(base: &str) -> io::Result<()> {
    let events = format!("{}.events", base);
    let commands = format!("{}.commands", base);
    loop {
        if std::path::Path::new(&events).exists()
            && std::path::Path::new(&commands).exists()
        {
            return Ok(());
        }
        log::info!("shm: rings not ready ({}, {}); retry in 1s", events, commands);
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
}
