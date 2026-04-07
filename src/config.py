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
