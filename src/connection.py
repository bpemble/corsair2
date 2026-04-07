"""IBKR Gateway connection management for Corsair v2."""

import logging
from typing import Callable, Optional

from ib_insync import IB

logger = logging.getLogger(__name__)


class IBKRConnection:
    """Manages the IBKR Gateway connection lifecycle."""

    def __init__(self, config):
        self.config = config
        self.ib = IB()
        self._on_disconnect_callback: Optional[Callable] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    def set_disconnect_callback(self, callback: Callable):
        self._on_disconnect_callback = callback

    async def connect(self) -> bool:
        """Connect to IBKR Gateway. Returns True on success."""
        host = self.config.account.gateway_host
        port = self.config.account.gateway_port
        client_id = self.config.account.client_id

        logger.info("Connecting to IBKR Gateway at %s:%d (client_id=%d)", host, port, client_id)

        try:
            await self.ib.connectAsync(host, port, clientId=client_id, timeout=30)
            self._connected = True

            # Register disconnect handler
            self.ib.disconnectedEvent += self._on_disconnect

            logger.info("Connected to IBKR Gateway. Server version: %s", self.ib.client.serverVersion())
            return True
        except Exception as e:
            logger.error("Failed to connect to IBKR Gateway: %s", e)
            self._connected = False
            return False

    async def disconnect(self):
        """Gracefully disconnect from IBKR Gateway."""
        if self.ib.isConnected():
            self.ib.disconnect()
        self._connected = False
        logger.info("Disconnected from IBKR Gateway")

    def _on_disconnect(self):
        """Called when gateway connection drops."""
        self._connected = False
        logger.critical("IBKR Gateway connection lost")
        if self._on_disconnect_callback:
            self._on_disconnect_callback()

