//! SHM ring + FIFO notification primitive.
//!
//! Two modes:
//!   - [`Ring::create_owner`] — broker creates the backing file +
//!     FIFO. Used by [`SHMServer`](crate::SHMServer).
//!   - [`Ring::open_client`] — trader maps the broker's existing
//!     file. Used by [`SHMClient`](crate::SHMClient) and currently
//!     by `corsair_trader/src/ipc/shm.rs` (will migrate to this
//!     crate in Phase 5B.5).

use memmap2::{MmapMut, MmapOptions};
use std::fs::OpenOptions;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::RawFd;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

pub const HDR_SIZE: usize = 16;
pub const DEFAULT_RING_CAPACITY: usize = 1 << 20; // 1 MiB

/// Unidirectional SPSC ring backed by mmap.
pub struct Ring {
    mmap: MmapMut,
    capacity: usize,
    notify_w_fd: Option<RawFd>,
    notify_r_fd: Option<RawFd>,
    pub frames_dropped: u64,
}

impl Ring {
    /// Server (broker) side: create the backing file at `path`,
    /// truncated to `HDR_SIZE + capacity`. Header bytes are zeroed
    /// so write_offset == read_offset == 0 (empty ring).
    ///
    /// If the file already exists, it's overwritten — caller's
    /// responsibility to ensure the previous broker isn't using it.
    pub fn create_owner(path: &Path, capacity: usize) -> io::Result<Self> {
        let total = HDR_SIZE + capacity;
        if let Some(parent) = path.parent() {
            if !parent.exists() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(path)?;
        file.set_len(total as u64)?;
        let mmap = unsafe { MmapOptions::new().len(total).map_mut(&file)? };
        let mut ring = Self {
            mmap,
            capacity,
            notify_w_fd: None,
            notify_r_fd: None,
            frames_dropped: 0,
        };
        // Zero the header explicitly. set_len above zero-fills new
        // bytes but if the file existed at the same size, it might
        // not — be safe.
        ring.set_write(0);
        ring.set_read(0);
        Ok(ring)
    }

    /// Client (trader) side: open an existing ring file.
    pub fn open_client(path: &Path, capacity: usize) -> io::Result<Self> {
        let total = HDR_SIZE + capacity;
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let mmap = unsafe { MmapOptions::new().len(total).map_mut(&file)? };
        Ok(Self {
            mmap,
            capacity,
            notify_w_fd: None,
            notify_r_fd: None,
            frames_dropped: 0,
        })
    }

    /// Backwards-compat alias for `open_client`. The trader binary's
    /// existing main.rs uses `Ring::open(path, capacity)`.
    pub fn open(path: &Path, capacity: usize) -> io::Result<Self> {
        Self::open_client(path, capacity)
    }

    /// Server side: create the FIFO at `<ring>.notify`.
    pub fn create_notify_fifo(path: &Path) -> io::Result<()> {
        let path_str = path.to_string_lossy();
        // mkfifo(3) — only fails if the path exists. We tolerate
        // that case (broker restart on existing files).
        let cstr = std::ffi::CString::new(path_str.as_bytes())
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
        unsafe {
            let r = libc::mkfifo(cstr.as_ptr(), 0o666);
            if r == 0 {
                Ok(())
            } else {
                let err = io::Error::last_os_error();
                if err.raw_os_error() == Some(libc::EEXIST) {
                    Ok(())
                } else {
                    Err(err)
                }
            }
        }
    }

    /// Open the notification FIFO. Same flags as Python:
    /// `O_RDWR | O_NONBLOCK`. `as_writer=true` registers as the
    /// notify-source; `false` registers as the wake-target.
    pub fn open_notify(&mut self, path: &Path, as_writer: bool) -> io::Result<()> {
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

    /// Producer side: signal the consumer.
    pub fn notify(&self) {
        if let Some(fd) = self.notify_w_fd {
            let buf = [0u8; 1];
            unsafe {
                libc::write(fd, buf.as_ptr() as *const _, 1);
            }
        }
    }

    /// Consumer side: drain pending notification bytes.
    pub fn drain_notify(&self) {
        if let Some(fd) = self.notify_r_fd {
            let mut buf = [0u8; 4096];
            unsafe {
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

    /// Producer side: write a complete frame. Returns false on
    /// "buffer full" (caller drops or retries).
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
        // Publish offset with release ordering.
        let new_w = w + n as u64;
        unsafe {
            let ptr = self.mmap.as_mut_ptr() as *mut AtomicU64;
            (*ptr).store(new_w, Ordering::Release);
        }
        self.notify();
        true
    }

    /// Consumer side: read everything available since last bump.
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

trait IntoRawFdKeep {
    fn into_raw_fd_keep(self) -> RawFd;
}

impl IntoRawFdKeep for std::fs::File {
    fn into_raw_fd_keep(self) -> RawFd {
        let fd = self.as_raw_fd();
        std::mem::forget(self);
        fd
    }
}

/// Wait for the broker to create both ring files at the given base
/// path. Returns once `<base>.events` and `<base>.commands` exist.
/// The trader's startup uses this to gate connection on broker boot.
pub async fn wait_for_rings(base: &str) -> io::Result<()> {
    let events = format!("{}.events", base);
    let commands = format!("{}.commands", base);
    loop {
        if std::path::Path::new(&events).exists()
            && std::path::Path::new(&commands).exists()
        {
            return Ok(());
        }
        log::info!("shm: rings not ready ({events}, {commands}); retry in 1s");
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn create_open_round_trip() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.events");
        let _server = Ring::create_owner(&path, 4096).unwrap();
        let _client = Ring::open_client(&path, 4096).unwrap();
    }

    #[test]
    fn write_then_read_via_two_handles() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ring.events");
        let mut server = Ring::create_owner(&path, 4096).unwrap();
        assert!(server.write_frame(b"hello"));
        let mut client = Ring::open_client(&path, 4096).unwrap();
        let data = client.read_available();
        assert_eq!(data, b"hello");
    }

    #[test]
    fn buffer_full_drops_frame() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ring.events");
        let mut ring = Ring::create_owner(&path, 64).unwrap();
        // Frame > capacity/2 fails immediately.
        let big = vec![0u8; 40];
        assert!(!ring.write_frame(&big));
        assert_eq!(ring.frames_dropped, 1);
    }
}
