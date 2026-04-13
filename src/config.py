"""Configuration loader for Corsair v2.

Loads a single YAML config file and provides typed access to all parameters.
No hardcoded values in the codebase — everything comes from config.
"""

import os
from types import SimpleNamespace
from typing import Any

import yaml


def _dict_to_namespace(d: Any) -> Any:
    """Recursively convert a dict to SimpleNamespace for dot-access."""
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _dict_to_namespace(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_dict_to_namespace(item) for item in d]
    return d


def load_config(path: str = "config/corsair_v2_config.yaml") -> SimpleNamespace:
    """Load and return the configuration as a nested SimpleNamespace.

    Environment variable overrides:
        CORSAIR_GATEWAY_HOST  -> account.gateway_host
        CORSAIR_GATEWAY_PORT  -> account.gateway_port
        CORSAIR_ACCOUNT_ID    -> account.account_id
    """
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh)

    config = _dict_to_namespace(raw)

    # Environment variable overrides for Docker deployment
    if os.environ.get("CORSAIR_GATEWAY_HOST"):
        config.account.gateway_host = os.environ["CORSAIR_GATEWAY_HOST"]
    if os.environ.get("CORSAIR_GATEWAY_PORT"):
        config.account.gateway_port = int(os.environ["CORSAIR_GATEWAY_PORT"])
    if os.environ.get("CORSAIR_ACCOUNT_ID"):
        config.account.account_id = os.environ["CORSAIR_ACCOUNT_ID"]

    return config


def _overlay_namespace(base, overrides):
    """Return a new SimpleNamespace with base attrs overlaid by overrides.

    Only replaces keys present in overrides; base keys not in overrides are
    kept. This lets a per-product block like ``quoting: {tick_size: 0.0005}``
    override just that field while inheriting everything else from the
    primary config's quoting block.
    """
    merged = SimpleNamespace(**vars(base))
    for k, v in vars(overrides).items():
        setattr(merged, k, v)
    return merged


def make_observe_config(base_config, observe_entry) -> SimpleNamespace:
    """Create a config view for an observe-only product.

    Replaces ``product`` and ``puts`` with the observe entry's values.
    If the observe entry also carries ``quoting`` or ``pricing`` blocks,
    those are *overlaid* onto the base config's blocks (per-key merge, not
    full replacement) so HG-specific tick_size / min_edge_points override
    ETH defaults while inheriting every other setting.
    """
    # Shallow copy the top-level namespace so we can replace blocks
    # without mutating the original.
    ns = SimpleNamespace(**vars(base_config))
    ns.product = observe_entry.product
    if hasattr(observe_entry, "puts"):
        ns.puts = observe_entry.puts
    # Overlay product-specific quoting/pricing overrides
    if hasattr(observe_entry, "quoting"):
        ns.quoting = _overlay_namespace(base_config.quoting, observe_entry.quoting)
    if hasattr(observe_entry, "pricing"):
        ns.pricing = _overlay_namespace(base_config.pricing, observe_entry.pricing)
    # Carry the snapshot_path through so main.py knows where to write.
    ns._observe_snapshot_path = getattr(observe_entry, "snapshot_path",
                                         f"data/{observe_entry.name.lower()}_chain_snapshot.json")
    ns._observe_name = observe_entry.name
    return ns
