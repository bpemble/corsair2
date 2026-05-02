//! Quick wire-byte dump for parity-checking against ib_insync.

use corsair_broker_ibkr_native::codec::encode_fields;
use corsair_broker_ibkr_native::requests::{req_account_updates, req_positions};

fn dump(name: &str, bytes: &[u8]) {
    print!("{name}: ");
    for b in bytes {
        print!("{b:02x}");
    }
    println!();
}

fn main() {
    dump("startApi (cId=13)  ", &encode_fields(&["71", "2", "13", ""]));
    dump("reqPositions       ", &req_positions());
    dump(
        "reqAccountUpdates  ",
        &req_account_updates(true, "DUP553657"),
    );
}
