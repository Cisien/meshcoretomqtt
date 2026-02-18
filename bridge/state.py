"""Shared mutable state container for the MeshCore bridge."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .serial_connection import SerialConnection
    from .auth_provider import AuthProvider

logger = logging.getLogger(__name__)


def parse_allowed_companions(remote_cfg: dict[str, Any]) -> set[str]:
    """Parse allowed_companions from config into a set of public keys."""
    companions_list = remote_cfg.get('allowed_companions', [])
    if not companions_list:
        return set()

    companions: set[str] = set()
    for key in companions_list:
        key = key.strip().upper()
        if len(key) == 64 and all(c in '0123456789ABCDEF' for c in key):
            companions.add(key)
        elif key:
            logger.warning(f"Invalid companion public key in allowlist: {key[:16]}...")

    if companions:
        logger.info(f"Remote serial enabled with {len(companions)} allowed companion(s)")
    return companions


class BridgeState:
    """All shared mutable state for the bridge."""

    def __init__(self, config: dict[str, Any], debug: bool = False) -> None:
        self.config = config
        self.debug = debug

        # Device info (populated during startup)
        self.repeater_name: str | None = None
        self.repeater_pub_key: str | None = None
        self.repeater_priv_key: str | None = None
        self.radio_info: str | None = None
        self.firmware_version: str | None = None
        self.model: str | None = None
        self.client_version: str = ""

        # Serial device (set during startup)
        self.device: SerialConnection | None = None

        # Auth provider (set during startup)
        self.auth: AuthProvider | None = None

        # MQTT state
        self.mqtt_clients: list[dict[str, Any]] = []
        self.mqtt_connected: bool = False
        self.connection_events: dict[int, threading.Event] = {}
        self.mqtt_manager: Any = None  # Set by bridge.__init__

        # Lifecycle
        self.should_exit: bool = False

        # Config-derived values
        self.global_iata: str = config.get('general', {}).get('iata', 'XXX')
        self.sync_time_at_start: bool = config.get('general', {}).get('sync_time', True)

        # Reconnect params
        self.reconnect_delay: float = 1.0
        self.max_reconnect_delay: float = 120.0
        self.reconnect_backoff: float = 1.5
        self.max_reconnect_attempts: int = 12

        # Token cache
        self.token_cache: dict[int, tuple[str, float]] = {}
        self.token_ttl: int = 3600

        # WebSocket ping threads
        self.ws_ping_threads: dict[int, dict[str, Any]] = {}

        # Remote serial config
        remote_cfg = config.get('remote_serial', {})
        self.remote_serial_enabled: bool = remote_cfg.get('enabled', False)
        self.remote_serial_allowed_companions: set[str] = parse_allowed_companions(remote_cfg)
        self.remote_serial_disallowed_commands: list[str] = remote_cfg.get(
            'disallowed_commands',
            ['get prv.key', 'set prv.key', 'erase', 'password']
        )
        self.remote_serial_nonce_ttl: int = remote_cfg.get('nonce_ttl', 120)
        self.remote_serial_nonces: dict[str, int] = {}
        self.remote_serial_command_timeout: int = remote_cfg.get('command_timeout', 10)

        # Statistics tracking
        self.stats: dict[str, Any] = {
            'start_time': time.time(),
            'packets_rx': 0,
            'packets_tx': 0,
            'packets_rx_prev': 0,
            'packets_tx_prev': 0,
            'bytes_processed': 0,
            'publish_failures': 0,
            'last_stats_log': time.time(),
            'reconnects': {},
            'device': {},
            'device_prev': {}
        }

        # Message parsing state
        self.last_raw: str | None = None

        logger.info("Configuration loaded from TOML")
