#!/usr/bin/env python3
from __future__ import annotations

__version__ = "1.1.0.0-preview"

import argparse
import logging
import signal

from config_loader import load_config
from bridge import MeshCoreBridge

# Initialize logging (console only) - will be reconfigured after config load
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument("--config", action="append", default=None,
                        help="Path to TOML config file (can be specified multiple times; overrides default config loading)")
    args: argparse.Namespace = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Reconfigure log level from config
    log_level_str = config.get('general', {}).get('log_level', 'INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    if args.debug:
        log_level = logging.DEBUG
    logger.setLevel(log_level)
    logging.getLogger().setLevel(log_level)

    bridge = MeshCoreBridge(config, debug=args.debug, version=__version__)

    # Ensure signals from systemd (SIGTERM) and ctrl-c (SIGINT) are handled
    signal.signal(signal.SIGTERM, bridge.handle_signal)
    signal.signal(signal.SIGINT, bridge.handle_signal)

    bridge.run()
