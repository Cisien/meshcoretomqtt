"""Configuration loading: TOML parsing, deep merging, and broker list merging."""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge_broker_lists(base_brokers: list[dict[str, Any]], override_brokers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge broker lists by name. Override brokers replace base brokers with the same name."""
    if not override_brokers:
        return base_brokers
    if not base_brokers:
        return override_brokers

    result = list(base_brokers)
    base_names = {b.get('name', ''): i for i, b in enumerate(result)}

    for broker in override_brokers:
        name = broker.get('name', '')
        if name and name in base_names:
            result[base_names[name]] = deep_merge(result[base_names[name]], broker)
        else:
            result.append(broker)

    return result


def _apply_override(config: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge an override dict into config, handling broker lists specially."""
    override_brokers = override.pop('broker', None)
    config_brokers = config.get('broker', [])
    config = deep_merge(config, override)
    if override_brokers is not None:
        config['broker'] = merge_broker_lists(config_brokers, override_brokers)
    return config


def _load_toml(path: str | Path) -> dict[str, Any]:
    """Load a single TOML file and return its contents as a dict."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def _load_config_dir(config: dict[str, Any], config_d: Path) -> dict[str, Any]:
    """Load all *.toml files from a config.d directory as overlays."""
    if not config_d.is_dir():
        return config
    for override_file in sorted(config_d.glob('*.toml')):
        logger.info(f"Loading config override: {override_file}")
        override = _load_toml(override_file)
        config = _apply_override(config, override)
    return config


def load_config(config_paths: list[str] | None = None) -> dict[str, Any]:
    """Load and merge TOML configuration.

    When no --config paths are provided (default):
      1. Load base config from /etc/mctomqtt/config.toml
      2. Overlay files from /etc/mctomqtt/config.d/*.toml (alphabetical)

    When --config paths are provided:
      Load only those files in order, each overlaying the previous.
      Default search paths and config.d directories are skipped.
    """
    if config_paths:
        config: dict = {}
        for path in config_paths:
            if not os.path.exists(path):
                logger.error(f"Config file not found: {path}")
                continue
            logger.info(f"Loading config: {path}")
            override = _load_toml(path)
            config = _apply_override(config, override)
        return config

    # Default: load system config
    config = {}
    base_path = '/etc/mctomqtt/config.toml'
    if os.path.exists(base_path):
        config = _load_toml(base_path)
        logger.info(f"Loaded base config from {base_path}")
    else:
        logger.warning(f"Base config not found at {base_path}, using defaults")

    # Load drop-in overrides
    config = _load_config_dir(config, Path('/etc/mctomqtt/config.d'))

    return config


def log_config_sources(config: dict[str, Any]) -> None:
    """Log configuration summary."""
    general = config.get('general', {})
    brokers = config.get('broker', [])
    serial_cfg = config.get('serial', {})

    logger.info(f"IATA: {general.get('iata', 'XXX')}")
    logger.info(f"Serial ports: {serial_cfg.get('ports', ['/dev/ttyACM0'])}")
    logger.info(f"Brokers configured: {len(brokers)}")

    for i, broker in enumerate(brokers):
        name = broker.get('name', f'broker-{i}')
        enabled = broker.get('enabled', False)
        server = broker.get('server', 'unknown')
        port = broker.get('port', 1883)
        logger.debug(f"  [{name}] enabled={enabled} server={server}:{port}")
