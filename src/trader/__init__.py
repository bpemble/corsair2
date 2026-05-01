"""Corsair trader process.

Receives gateway events from the broker (current corsair) over a Unix
socket, maintains a local price book, and (eventually) makes quoting
decisions. v1 only logs events for IPC validation; v2 will replicate
broker decisions for parity comparison; v3 will take over order
placement.

See ``docs/architecture/mm_service_split.md`` for the full plan.
"""
