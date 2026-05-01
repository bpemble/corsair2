//! IPC layer: SHM rings + FIFO notification + msgpack frame protocol.
//! Mirror of Python's `src/ipc/{protocol,shm}.py`.

pub mod protocol;
pub mod shm;
