"""Trader process entry point.

Phase 1+2 scope:
  - Connect to broker IPC.
  - Receive every event the broker forwards.
  - Maintain a per-symbol price book + active-orders index from order acks.
  - Log every event to ``logs-paper/trader_events-YYYY-MM-DD.jsonl`` with
    a receive-side timestamp, so we can measure IPC latency offline
    (broker stamps ts_ns at emit; trader stamps recv_ns on receipt).
  - Periodic telemetry back to broker (event counts, IPC latency p50/p99).

NOT in this scope:
  - Quoting decisions (Phase 2c)
  - placeOrder commands (Phase 3)

Run: ``python3 -m src.trader.main``. Container command in
``docker-compose.yml``.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import Counter, deque
from datetime import date, datetime, timezone

from ..ipc import IPCClient, DEFAULT_SOCKET_PATH

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("corsair.trader")

# Where event JSONL is written. Same dir as the broker's paper streams
# so analysis tools find it next to ``fills``, ``hedge_trades`` etc.
EVENTS_LOG_DIR = "/app/logs-paper"
TELEMETRY_INTERVAL_S = 10.0


class TraderState:
    """Trader's local view of the world, populated from broker events."""

    def __init__(self) -> None:
        # Per-option price book: (strike, expiry, right) -> dict of latest tick
        self.options: dict = {}
        self.underlying_price: float = 0.0
        # Trader's view of OUR resting orders, keyed by orderId.
        self.active_orders: dict = {}
        # Most recent vol surface params per expiry.
        self.vol_surface: dict = {}
        # Kill state: source -> reason. Empty means quoting allowed.
        self.kills: dict = {}
        self.weekend_paused: bool = False
        # Telemetry
        self.event_counts: Counter = Counter()
        self.ipc_latency_us: deque[int] = deque(maxlen=2000)
        self.last_event_ts: float = 0.0


class EventLogger:
    """Append-only JSONL writer for received events. Lock-free single-thread."""

    def __init__(self, log_dir: str) -> None:
        self._log_dir = log_dir
        self._fp = None
        self._date = None

    def write(self, msg: dict, recv_ns: int) -> None:
        today = date.today().strftime("%Y-%m-%d")
        if today != self._date:
            self._roll(today)
        if self._fp is None:
            return
        rec = {
            "recv_ts": datetime.now(timezone.utc).isoformat(),
            "recv_ns": recv_ns,
            "event": msg,
        }
        try:
            self._fp.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            logger.exception("event log write failed")

    def _roll(self, day: str) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
        self._date = day
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            path = os.path.join(self._log_dir, f"trader_events-{day}.jsonl")
            self._fp = open(path, "a", buffering=1)
            logger.info("event log opened: %s", path)
        except Exception:
            logger.exception("event log open failed")
            self._fp = None

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None


class Trader:
    def __init__(self, socket_path: str) -> None:
        self.client = IPCClient(socket_path)
        self.state = TraderState()
        self.event_log = EventLogger(EVENTS_LOG_DIR)

    async def on_event(self, msg: dict) -> None:
        recv_ns = time.monotonic_ns()
        ev_type = msg.get("type", "?")
        self.state.event_counts[ev_type] += 1
        self.state.last_event_ts = time.monotonic()

        # IPC latency: broker stamps "ts_ns" at emit (loop monotonic_ns on
        # the broker side); trader's receive monotonic_ns is on a different
        # process so the two clocks aren't directly comparable. Wall-clock
        # delta from the broker's "ts" field is comparable across processes.
        broker_ts_ns = msg.get("ts_ns")
        if isinstance(broker_ts_ns, int):
            wall_recv_ns = time.time_ns()
            lat_us = max(0, (wall_recv_ns - broker_ts_ns) // 1000)
            if 0 <= lat_us < 5_000_000:  # cap at 5s; ignore clock skew
                self.state.ipc_latency_us.append(lat_us)

        # Update local state per type.
        try:
            self._apply(msg)
        except Exception:
            logger.exception("apply failed for %s", ev_type)

        self.event_log.write(msg, recv_ns)

    def _apply(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "tick":
            key = (msg["strike"], msg["expiry"], msg["right"])
            self.state.options[key] = msg
        elif t == "underlying_tick":
            self.state.underlying_price = float(msg.get("price", 0.0))
        elif t == "order_ack":
            oid = msg.get("orderId")
            if oid is not None:
                if msg.get("status") in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    self.state.active_orders.pop(oid, None)
                else:
                    self.state.active_orders[oid] = msg
        elif t == "fill":
            # No state mutation here in Phase 2 — broker handles position
            # bookkeeping. Just log via on_event's caller.
            pass
        elif t == "vol_surface":
            self.state.vol_surface[msg.get("expiry")] = msg
        elif t == "kill":
            self.state.kills[msg.get("source", "?")] = msg.get("reason", "?")
        elif t == "resume":
            self.state.kills.pop(msg.get("source", "?"), None)
        elif t == "weekend_pause":
            self.state.weekend_paused = bool(msg.get("paused", False))
        elif t == "snapshot":
            # Initial state seed from broker on (re)connect. Phase 3+.
            pass
        elif t == "hello":
            logger.warning("broker hello: %s", msg)
        else:
            logger.debug("unknown event type: %s", t)

    async def telemetry_loop(self) -> None:
        """Send periodic stats back to the broker."""
        while True:
            await asyncio.sleep(TELEMETRY_INTERVAL_S)
            if not self.client.connected:
                continue
            lats = sorted(self.state.ipc_latency_us)
            n = len(lats)
            p50 = lats[n // 2] if n else None
            p99 = lats[int(n * 0.99)] if n else None
            payload = {
                "type": "telemetry",
                "ts_ns": time.time_ns(),
                "events": dict(self.state.event_counts.most_common()),
                "ipc_p50_us": p50,
                "ipc_p99_us": p99,
                "ipc_n": n,
                "n_options": len(self.state.options),
                "n_active_orders": len(self.state.active_orders),
                "n_vol_expiries": len(self.state.vol_surface),
                "killed": list(self.state.kills.keys()),
                "weekend_paused": self.state.weekend_paused,
            }
            self.client.send(payload)
            logger.info(
                "telemetry: events=%d ipc_p50=%sus ipc_p99=%sus opts=%d orders=%d",
                sum(self.state.event_counts.values()),
                p50, p99,
                len(self.state.options),
                len(self.state.active_orders),
            )

    async def run(self) -> None:
        # Send welcome handshake when first connected.
        async def _on_event(msg):
            await self.on_event(msg)

        # Register a startup hook that fires after first connection. Simplest:
        # poll connection state in a small wrapper task and send when up.
        async def _send_welcome_when_connected():
            while not self.client.connected:
                await asyncio.sleep(0.05)
            self.client.send({
                "type": "welcome",
                "ts_ns": time.time_ns(),
                "trader_version": "v1",
            })
            logger.warning("trader sent welcome")

        client_task = asyncio.create_task(self.client.run(_on_event))
        welcome_task = asyncio.create_task(_send_welcome_when_connected())
        telemetry_task = asyncio.create_task(self.telemetry_loop())

        try:
            await asyncio.gather(client_task, telemetry_task, welcome_task)
        finally:
            self.event_log.close()


async def main() -> None:
    socket_path = os.environ.get("CORSAIR_IPC_SOCKET", DEFAULT_SOCKET_PATH)
    logger.info("Trader starting; broker socket=%s", socket_path)

    trader = Trader(socket_path)
    stop = asyncio.Event()

    def _signal_handler(*_):
        logger.info("shutdown requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    runner = asyncio.create_task(trader.run(), name="trader-run")
    await stop.wait()
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
