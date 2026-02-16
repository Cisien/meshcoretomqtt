#!/usr/bin/env python3
from __future__ import annotations

__version__ = "1.0.7.0"

import sys
import os
import json
import serial
import threading
import argparse
import re
import time
import calendar
import logging
import signal
import random
import subprocess
from datetime import datetime
from time import sleep
from pathlib import Path
from typing import Any
from auth_token import create_auth_token, read_private_key_file, verify_auth_token, decode_token_payload

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed. Install with:")
    print("pip install paho-mqtt")
    sys.exit(1)


from config_loader import deep_merge, load_config, log_config_sources, merge_broker_lists


# Regex patterns for message parsing
RAW_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U RAW: (.*)")
PACKET_PATTERN = re.compile(
    r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U: (RX|TX), len=(\d+) \(type=(\d+), route=([A-Z]), payload_len=(\d+)\)"
    r"(?: SNR=(-?\d+) RSSI=(-?\d+) score=(\d+)( time=(\d+))? hash=([0-9A-F]+)(?: \[(.*)\])?)?"
)

# Initialize logging (console only) - will be reconfigured after config load
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

class MeshCoreBridge:
    last_raw: str | None = None

    def __init__(self, config: dict[str, Any], debug: bool = False) -> None:
        self.debug = debug
        self.config = config
        self.repeater_name = None
        self.repeater_pub_key = None
        self.repeater_priv_key = None
        self.radio_info = None
        self.firmware_version = None
        self.model = None
        self.client_version = self._load_client_version()
        self.ser = None
        self.ser_lock = threading.Lock()  # Lock for thread-safe serial access
        self.mqtt_clients = []
        self.mqtt_connected = False
        self.connection_events = {}  # Track connection completion per broker
        self.should_exit = False
        self.global_iata = config.get('general', {}).get('iata', 'XXX')
        self.reconnect_delay = 1.0  # Start with 1 second
        self.max_reconnect_delay = 120.0  # Max 2 minutes
        self.reconnect_backoff = 1.5  # Exponential backoff multiplier
        self.reconnect_attempts = {}  # Track consecutive failed reconnect attempts per broker
        self.max_reconnect_attempts = 12  # Exit after this many consecutive failures
        self.token_cache = {}  # Cache tokens with their creation time
        self.token_ttl = 3600  # 1 hour token TTL
        self.ws_ping_threads = {}  # Track WebSocket ping threads per broker
        self.sync_time_at_start = config.get('general', {}).get('sync_time', True)

        # Remote serial configuration
        remote_cfg = config.get('remote_serial', {})
        self.remote_serial_enabled = remote_cfg.get('enabled', False)
        self.remote_serial_allowed_companions = self._parse_allowed_companions(remote_cfg)
        self.remote_serial_disallowed_commands = remote_cfg.get('disallowed_commands', ['get prv.key', 'set prv.key', 'erase', 'password'])
        self.remote_serial_nonce_ttl = remote_cfg.get('nonce_ttl', 120)
        self.remote_serial_nonces = {}  # {nonce: timestamp} for replay protection
        self.remote_serial_command_timeout = remote_cfg.get('command_timeout', 10)

        # Statistics tracking
        self.stats = {
            'start_time': time.time(),
            'packets_rx': 0,
            'packets_tx': 0,
            'packets_rx_prev': 0,
            'packets_tx_prev': 0,
            'bytes_processed': 0,
            'publish_failures': 0,
            'last_stats_log': time.time(),
            'reconnects': {},  # {broker_idx: [timestamp1, timestamp2, ...]}
            'device': {},  # Device stats from serial (battery, uptime, errors, etc.)
            'device_prev': {}  # Previous device stats for delta calculation
        }

        logger.info("Configuration loaded from TOML")

    def _load_client_version(self) -> str:
        """Load client version from __version__ and optionally append git hash from .version_info"""
        version = __version__
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            version_file = os.path.join(script_dir, '.version_info')
            if os.path.exists(version_file):
                with open(version_file, 'r') as f:
                    version_data = json.load(f)
                    git_hash = version_data.get('git_hash', '')
                    if git_hash and git_hash != 'unknown':
                        return f"meshcoretomqtt/{version}-{git_hash}"
        except Exception as e:
            logger.debug(f"Could not load version info: {e}")
        return f"meshcoretomqtt/{version}"

    def _parse_allowed_companions(self, remote_cfg: dict[str, Any]) -> set[str]:
        """Parse allowed_companions from config into a set of public keys"""
        companions_list = remote_cfg.get('allowed_companions', [])
        if not companions_list:
            return set()

        companions = set()
        for key in companions_list:
            key = key.strip().upper()
            # Validate it's a valid 64-char hex public key
            if len(key) == 64 and all(c in '0123456789ABCDEF' for c in key):
                companions.add(key)
            elif key:  # Only warn if non-empty
                logger.warning(f"Invalid companion public key in allowlist: {key[:16]}...")

        if companions:
            logger.info(f"Remote serial enabled with {len(companions)} allowed companion(s)")
        return companions

    def _is_command_allowed(self, command: str) -> tuple[bool, str | None]:
        """Check if a command is allowed (not in disallowed list)"""
        cmd_lower = command.strip().lower()

        for disallowed in self.remote_serial_disallowed_commands:
            if cmd_lower.startswith(disallowed.lower()):
                return False, disallowed

        return True, None

    def _get_broker_config(self, broker_idx: int) -> dict[str, Any]:
        """Get broker config by index into the broker list"""
        brokers = self.config.get('broker', [])
        if broker_idx < len(brokers):
            return brokers[broker_idx]
        return {}

    def resolve_topic_template(self, template: str, broker_idx: int | None = None) -> str:
        """Resolve topic template with {IATA} and {PUBLIC_KEY} placeholders"""
        if not template:
            return template

        # Get IATA - broker-specific or global
        iata = self.global_iata
        if broker_idx is not None:
            broker = self._get_broker_config(broker_idx)
            broker_topics = broker.get('topics', {})
            broker_iata = broker_topics.get('iata', '')
            if broker_iata:
                iata = broker_iata

        # Replace template variables
        resolved = template.replace('{IATA}', iata)
        resolved = resolved.replace('{PUBLIC_KEY}', self.repeater_pub_key if self.repeater_pub_key else 'UNKNOWN')
        return resolved

    def get_topic(self, topic_type: str, broker_idx: int | None = None) -> str:
        """Get topic with template resolution, checking broker-specific override first"""
        # Check broker-specific topic override
        if broker_idx is not None:
            broker = self._get_broker_config(broker_idx)
            broker_topics = broker.get('topics', {})
            broker_topic = broker_topics.get(topic_type, '')
            if broker_topic:
                return self.resolve_topic_template(broker_topic, broker_idx)

        # Fall back to global topic
        topics = self.config.get('topics', {})
        global_topic = topics.get(topic_type, '')
        return self.resolve_topic_template(global_topic, broker_idx)

    def sanitize_client_id(self, name: str) -> str:
        """Convert repeater name to valid MQTT client ID"""
        # Use first broker's client_id_prefix or default
        brokers = self.config.get('broker', [])
        prefix = "meshcore_"
        if brokers:
            prefix = brokers[0].get('client_id_prefix', 'meshcore_')
        client_id = prefix + name.replace(" ", "_")
        client_id = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)
        return client_id[:23]

    def generate_auth_credentials(self, broker_idx: int, force_refresh: bool = False) -> tuple[str | None, str | None]:
        """Generate authentication credentials for a broker on-demand"""
        broker = self._get_broker_config(broker_idx)
        auth = broker.get('auth', {})
        auth_method = auth.get('method', 'none')

        if auth_method == 'token':
            if not self.repeater_priv_key:
                logger.error(f"[{broker.get('name', broker_idx)}] Private key not available from device for auth token")
                return None, None

            # Check if we have a cached token that's still fresh
            current_time = time.time()
            if not force_refresh and broker_idx in self.token_cache:
                cached_token, created_at = self.token_cache[broker_idx]
                age = current_time - created_at
                if age < (self.token_ttl - 300):  # Use cached token if it has >5min remaining
                    logger.debug(f"[{broker.get('name', broker_idx)}] Using cached auth token (age: {age:.0f}s)")
                    username = f"v1_{self.repeater_pub_key.upper()}"
                    return username, cached_token

            # Generate fresh token
            try:
                username = f"v1_{self.repeater_pub_key.upper()}"
                audience = auth.get('audience', '')

                # Security check: Only include email/owner if using TLS with verification
                tls_cfg = broker.get('tls', {})
                use_tls = tls_cfg.get('enabled', False)
                tls_verify = tls_cfg.get('verify', True)
                secure_connection = use_tls and tls_verify

                owner = auth.get('owner', '')
                email = auth.get('email', '')

                claims = {}
                if audience:
                    claims['aud'] = audience

                if secure_connection:
                    if owner:
                        claims['owner'] = owner
                    if email:
                        claims['email'] = email.lower()
                else:
                    if owner or email:
                        logger.debug(f"[{broker.get('name', broker_idx)}] Skipping email/owner in JWT - TLS and TLS verify must both be enabled")

                claims['client'] = self.client_version

                # Generate token with 1 hour expiry
                password = create_auth_token(self.repeater_pub_key, self.repeater_priv_key, expiry_seconds=self.token_ttl, **claims)
                self.token_cache[broker_idx] = (password, current_time)
                logger.debug(f"[{broker.get('name', broker_idx)}] Generated fresh auth token (1h expiry)")
                return username, password
            except Exception as e:
                logger.error(f"[{broker.get('name', broker_idx)}] Failed to generate auth token: {e}")
                return None, None
        elif auth_method == 'password':
            username = auth.get('username', '')
            password = auth.get('password', '')
            return username, password
        else:
            # No auth
            return '', ''

    def connect_serial(self) -> bool:
        serial_cfg = self.config.get('serial', {})
        ports = serial_cfg.get('ports', ['/dev/ttyACM0'])
        baud_rate = serial_cfg.get('baud_rate', 115200)
        timeout = serial_cfg.get('timeout', 2)

        for port in ports:
            try:
                # Close any existing serial handle before creating a new one
                self.close_serial()

                self.ser = serial.Serial(
                    port=port,
                    baudrate=baud_rate,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    bytesize=serial.EIGHTBITS,
                    timeout=timeout,
                    rtscts=False
                )
                self.ser.write(b"\r\n\r\n")
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
                logger.info(f"Connected to {port}")
                return True
            except (serial.SerialException, OSError) as e:
                logger.warning(f"Failed to connect to {port}: {str(e)}")
                continue
        logger.error("Failed to connect to any serial port")
        return False

    def close_serial(self) -> None:
        """Close and clear the current serial handle if present."""
        try:
            if self.ser:
                try:
                    if getattr(self.ser, "is_open", False):
                        logger.debug("Closing serial connection")
                        self.ser.close()
                except Exception:
                    pass
        finally:
            self.ser = None

    def set_repeater_time(self) -> None:
        if not self.ser:
            return False

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        epoc_time = int(calendar.timegm(time.gmtime()))
        timecmd=f'time {epoc_time}\r\n'
        self.ser.write(timecmd.encode())
        logger.debug(f"Sent '{timecmd}' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw response: {response}")

    def get_repeater_name(self) -> bool:
        if not self.ser:
            return False

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(b"get name\r\n")
        logger.debug("Sent 'get name' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            name = response.split("-> >")[1].strip()
            if '\n' in name:
                name = name.split('\n')[0]
            name = name.replace('\r', '').strip()
            self.repeater_name = name
            logger.info(f"Repeater name: {self.repeater_name}")
            return True

        logger.error("Failed to get repeater name from response")
        return False

    def get_repeater_pubkey(self) -> bool:
        if not self.ser:
            return False

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(b"get public.key\r\n")
        logger.debug("Sent 'get public.key' command")

        sleep(1.0)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            pub_key = response.split("-> >")[1].strip()
            if '\n' in pub_key:
                pub_key = pub_key.split('\n')[0]
            pub_key_clean = pub_key.replace(' ', '').replace('\r', '').replace('\n', '')

            # Validate public key format (should be 64 hex characters)
            if not pub_key_clean or len(pub_key_clean) != 64 or not all(c in '0123456789ABCDEFabcdef' for c in pub_key_clean):
                logger.error(f"Invalid public key format: {repr(pub_key_clean)} (extracted from: {repr(pub_key)})")
                return False

            # Normalize to uppercase
            self.repeater_pub_key = pub_key_clean.upper()
            logger.info(f"Repeater pub key: {self.repeater_pub_key}")
            return True

        logger.error("Failed to get repeater pub key from response")
        return False

    def get_repeater_privkey(self) -> bool:
        if not self.ser:
            return False

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(b"get prv.key\r\n")
        logger.debug("Sent 'get prv.key' command")

        sleep(1.0)
        response = self.ser.read_all().decode(errors='replace')
        if "-> >" in response:
            priv_key = response.split("-> >")[1].strip()
            if '\n' in priv_key:
                priv_key = priv_key.split('\n')[0]

            priv_key_clean = priv_key.replace(' ', '').replace('\r', '').replace('\n', '')
            if len(priv_key_clean) == 128:
                try:
                    int(priv_key_clean, 16)  # Validate it's hex
                    self.repeater_priv_key = priv_key_clean
                    logger.info(f"Repeater priv key: {self.repeater_priv_key[:4]}... (truncated for security)")
                    return True
                except ValueError as e:
                    logger.error(f"Response not valid hex: {priv_key_clean[:32]}... Error: {e}")
            else:
                logger.error(f"Response wrong length: {len(priv_key_clean)} (expected 128)")

        logger.error("Failed to get repeater priv key from response - command may not be supported by firmware")
        return False

    def get_radio_info(self) -> str | None:
        """Query the repeater for radio information"""
        if not self.ser:
            return None

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(b"get radio\r\n")
        logger.debug("Sent 'get radio' command")

        sleep(0.5)  # Adjust delay if necessary
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw radio response: {response}")

        if "-> >" in response:
            radio_info = response.split("-> >")[1].strip()
            if '\n' in radio_info:
                radio_info = radio_info.split('\n')[0]
            logger.debug(f"Parsed radio info: {radio_info}")
            return radio_info

        logger.error("Failed to get radio info from response")
        return None

    def get_firmware_version(self) -> str | None:
        """Query the repeater for firmware version"""
        if not self.ser:
            return None

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(b"ver\r\n")
        logger.debug("Sent 'ver' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw version response: {response}")

        # Response format: "ver\n  -> 1.8.2-dev-834c700 (Build: 04-Sep-2025)\n"
        if "-> " in response:
            version = response.split("-> ", 1)[1]
            version = version.split('\n')[0].replace('\r', '').strip()
            logger.info(f"Firmware version: {version}")
            return version

        logger.warning("Failed to get firmware version from response")
        return None

    def get_board_type(self) -> str | None:
        """Query the repeater for board/hardware type"""
        if not self.ser:
            return None

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.ser.write(b"board\r\n")
        logger.debug("Sent 'board' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw board response: {response}")

        # Response format: "board\n  -> Station G2\n"
        if "-> " in response:
            board_type = response.split("-> ", 1)[1]
            board_type = board_type.split('\n')[0].replace('\r', '').strip()
            if board_type == "Unknown command":
                board_type = "unknown"
            logger.info(f"Board type: {board_type}")
            return board_type

        logger.warning("Failed to get board type from response")
        return None

    def get_device_stats(self) -> dict[str, Any]:
        """Query the repeater for device statistics (battery, uptime, errors, queue, radio stats)"""
        if not self.ser:
            return {}

        stats = {}

        with self.ser_lock:
            # Get stats-core: battery_mv, uptime_secs, errors, queue_len
            self.ser.flushInput()
            self.ser.flushOutput()
            self.ser.write(b"stats-core\r\n")
            logger.debug("Sent 'stats-core' command")

            sleep(0.5)
            response = self.ser.read_all().decode(errors='replace')
            logger.debug(f"Raw stats-core response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    core_stats = json.loads(json_str)
                    if 'battery_mv' in core_stats:
                        stats['battery_mv'] = core_stats['battery_mv']
                    if 'uptime_secs' in core_stats:
                        stats['uptime_secs'] = core_stats['uptime_secs']
                    if 'errors' in core_stats:
                        stats['errors'] = core_stats['errors']
                    if 'queue_len' in core_stats:
                        stats['queue_len'] = core_stats['queue_len']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-core: {e}")

            # Get stats-radio: noise_floor, tx_air_secs, rx_air_secs
            self.ser.flushInput()
            self.ser.flushOutput()
            self.ser.write(b"stats-radio\r\n")
            logger.debug("Sent 'stats-radio' command")

            sleep(0.5)
            response = self.ser.read_all().decode(errors='replace')
            logger.debug(f"Raw stats-radio response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    radio_stats = json.loads(json_str)
                    if 'noise_floor' in radio_stats:
                        stats['noise_floor'] = radio_stats['noise_floor']
                    if 'tx_air_secs' in radio_stats:
                        stats['tx_air_secs'] = radio_stats['tx_air_secs']
                    if 'rx_air_secs' in radio_stats:
                        stats['rx_air_secs'] = radio_stats['rx_air_secs']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-radio: {e}")

        return stats

    def _websocket_ping_loop(self, broker_idx: int, mqtt_client: mqtt.Client, transport: str) -> None:
        """Send WebSocket PING frames periodically to keep connection alive"""
        if transport != "websockets":
            return

        ping_interval = 45  # Send WebSocket ping every 45 seconds

        while broker_idx in self.ws_ping_threads and self.ws_ping_threads[broker_idx].get('active', False):
            sleep(ping_interval)

            try:
                # Access the underlying WebSocket object in paho-mqtt
                if hasattr(mqtt_client, '_sock') and mqtt_client._sock:
                    sock = mqtt_client._sock
                    # Check if it's a WebSocket
                    if hasattr(sock, 'ping'):
                        sock.ping()
                        logger.debug(f"[{broker_idx}] Sent WebSocket PING")
            except Exception as e:
                logger.debug(f"[{broker_idx}] WebSocket PING failed: {e}")
                # Don't break the loop - connection might recover

    def _stats_logging_loop(self) -> None:
        """Log statistics every 5 minutes"""
        stats_interval = 300

        while not self.should_exit:
            sleep(stats_interval)

            if self.should_exit:
                break

            # Fetch fresh device stats from serial
            logger.debug("[STATS] Fetching fresh device stats from serial...")
            device_stats = self.get_device_stats()
            if device_stats:
                self.stats['device'] = device_stats
                logger.debug(f"[STATS] Updated device stats: {device_stats}")
                # Publish updated status with new stats
                self.publish_status("online")
            else:
                logger.debug("[STATS] No device stats received")

            # Calculate uptime
            uptime_seconds = int(time.time() - self.stats['start_time'])
            uptime_hours = uptime_seconds // 3600
            uptime_minutes = (uptime_seconds % 3600) // 60

            if uptime_hours > 0:
                uptime_str = f"{uptime_hours}h {uptime_minutes}m"
            else:
                uptime_str = f"{uptime_minutes}m"

            # Calculate data volume with appropriate units
            bytes_actual = self.stats['bytes_processed']
            if bytes_actual < 1024:
                data_str = f"{bytes_actual}B"
            elif bytes_actual < 1024 * 1024:
                data_str = f"{bytes_actual / 1024:.1f}KB"
            elif bytes_actual < 1024 * 1024 * 1024:
                data_str = f"{bytes_actual / (1024 * 1024):.1f}MB"
            else:
                data_str = f"{bytes_actual / (1024 * 1024 * 1024):.2f}GB"

            total_brokers = len(self.mqtt_clients)
            connected_brokers = sum(1 for info in self.mqtt_clients if info.get('connected', False))

            # Calculate packets per minute over the last interval (5 minutes)
            time_elapsed = time.time() - self.stats['last_stats_log']
            packets_rx_delta = self.stats['packets_rx'] - self.stats['packets_rx_prev']
            packets_tx_delta = self.stats['packets_tx'] - self.stats['packets_tx_prev']
            packets_per_min = ((packets_rx_delta + packets_tx_delta) / time_elapsed) * 60 if time_elapsed > 0 else 0

            # Store current counts for next interval
            self.stats['packets_rx_prev'] = self.stats['packets_rx']
            self.stats['packets_tx_prev'] = self.stats['packets_tx']

            # Prune reconnect timestamps older than 24 hours and build reconnect stats
            current_time = time.time()
            cutoff_time = current_time - 86400  # 24 hours in seconds
            reconnect_stats = []

            for broker_idx in sorted(self.stats['reconnects'].keys()):
                # Prune old timestamps
                self.stats['reconnects'][broker_idx] = [
                    ts for ts in self.stats['reconnects'][broker_idx] if ts > cutoff_time
                ]

                # Count reconnects in last 24 hours
                reconnect_count = len(self.stats['reconnects'][broker_idx])
                if reconnect_count > 0:
                    broker = self._get_broker_config(broker_idx)
                    name = broker.get('name', f'broker-{broker_idx}')
                    reconnect_stats.append(f"{name}:{reconnect_count}")

            reconnect_str = ", ".join(reconnect_stats) if reconnect_stats else "none"

            # Log the main stats
            logger.info(
                f"[SERVICE] Uptime: {uptime_str} | "
                f"RX/TX: {self.stats['packets_rx']}/{self.stats['packets_tx']} (5m: {packets_per_min:.1f}/min) | "
                f"RX bytes: {data_str} | "
                f"MQTT: {connected_brokers}/{total_brokers} | "
                f"Reconnects/24h: {reconnect_str} | "
                f"Failures: {self.stats['publish_failures']}"
            )

            # Log device stats separately if available
            if self.stats['device']:
                ds = self.stats['device']
                parts = []

                if 'noise_floor' in ds:
                    parts.append(f"Noise: {ds['noise_floor']}dB")

                # Radio airtime stats with utilization (calculated over interval, not total uptime)
                if 'tx_air_secs' in ds and 'rx_air_secs' in ds and 'uptime_secs' in ds:
                    tx_secs_total = ds['tx_air_secs']
                    rx_secs_total = ds['rx_air_secs']
                    uptime_secs = ds['uptime_secs']

                    # Calculate delta from previous reading
                    prev = self.stats.get('device_prev', {})
                    if prev and 'tx_air_secs' in prev and 'rx_air_secs' in prev and 'uptime_secs' in prev:
                        # Delta calculation (airtime since last reading)
                        tx_delta = tx_secs_total - prev['tx_air_secs']
                        rx_delta = rx_secs_total - prev['rx_air_secs']
                        uptime_delta = uptime_secs - prev['uptime_secs']

                        if uptime_delta > 0:
                            tx_util = (tx_delta / uptime_delta) * 100
                            rx_util = (rx_delta / uptime_delta) * 100
                            parts.append(f"Air (5m): Tx {tx_delta:.1f}s ({tx_util:.2f}%), Rx {rx_delta:.1f}s ({rx_util:.2f}%)")
                        else:
                            parts.append(f"Air (5m): Tx {tx_delta:.1f}s, Rx {rx_delta:.1f}s")
                    else:
                        # Initial reading - show totals
                        parts.append(f"Air (5m): Tx {tx_secs_total}s, Rx {rx_secs_total}s")
                elif 'tx_air_secs' in ds and 'rx_air_secs' in ds:
                    parts.append(f"Air (5m): Tx {ds['tx_air_secs']}s, Rx {ds['rx_air_secs']}s")

                # Battery
                if 'battery_mv' in ds:
                    parts.append(f"Battery: {ds['battery_mv']}mV")

                # Device uptime
                if 'uptime_secs' in ds:
                    dev_uptime_secs = ds['uptime_secs']
                    dev_uptime_hours = dev_uptime_secs // 3600
                    dev_uptime_minutes = (dev_uptime_secs % 3600) // 60

                    if dev_uptime_hours > 0:
                        dev_uptime_str = f"{dev_uptime_hours}h {dev_uptime_minutes}m"
                    else:
                        dev_uptime_str = f"{dev_uptime_minutes}m"

                    parts.append(f"Uptime: {dev_uptime_str}")

                # Errors
                if 'errors' in ds:
                    parts.append(f"Errors: {ds['errors']}")

                # Queue
                if 'queue_len' in ds:
                    parts.append(f"Queue: {ds['queue_len']}")

                if parts:
                    logger.info(f"[DEVICE] {' | '.join(parts)}")

            # Save current device stats as previous for next interval calculation
            if self.stats['device']:
                self.stats['device_prev'] = self.stats['device'].copy()

            self.stats['last_stats_log'] = time.time()

    def on_mqtt_connect(self, client: mqtt.Client, userdata: dict[str, Any] | None, flags: Any, rc: int, properties: Any = None) -> None:
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        broker_idx = userdata.get('broker_idx', None) if userdata else None

        # Signal that this broker has completed its connection attempt
        if broker_idx in self.connection_events:
            self.connection_events[broker_idx].set()

        if rc == 0:
            # Reset reconnect delay on successful connection
            self.reconnect_delay = 1.0

            # Find the mqtt_info for this broker
            mqtt_info = None
            for info in self.mqtt_clients:
                if info['broker_idx'] == broker_idx:
                    mqtt_info = info
                    break

            if not mqtt_info:
                logger.error(f"[{broker_name}] on_connect fired but broker not in mqtt_clients list")
                return

            current_time = time.time()
            was_connected = mqtt_info.get('connected', False)
            is_first_connect = mqtt_info.get('connect_time', 0) == 0

            # Set connected state
            mqtt_info['connected'] = True
            mqtt_info['connecting_since'] = 0  # Clear connecting timestamp
            mqtt_info['connect_time'] = current_time

            if was_connected and not is_first_connect:
                logger.info(f"[{broker_name}] Reconnected to broker")
            elif is_first_connect:
                logger.info(f"[{broker_name}] Connected to broker")
            else:
                logger.debug(f"[{broker_name}] Connection state updated")

            # Track global connected state
            if not self.mqtt_connected:
                self.mqtt_connected = True

            # Publish online status
            status_topic = self.get_topic("status", broker_idx)
            status_payload = json.dumps(self.build_status_message("online"))
            broker = self._get_broker_config(broker_idx)
            qos = broker.get('qos', 0)
            retain = broker.get('retain', True)

            try:
                result = client.publish(status_topic, status_payload, qos=qos, retain=retain)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    pass  # Don't reset failed_attempts here - let it reset after 120s of stability
            except Exception as e:
                logger.error(f"[{broker_name}] Failed to publish online status: {e}")

            # Subscribe to remote serial commands (if enabled)
            self._subscribe_serial_commands(client, broker_idx)
        else:
            logger.error(f"[{broker_name}] Connection failed with code: {rc}")


    def on_mqtt_disconnect(self, client: mqtt.Client, userdata: dict[str, Any] | None, disconnect_flags: Any, reason_code: Any, properties: Any) -> None:
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        broker_idx = userdata.get('broker_idx', None) if userdata else None

        # Stop WebSocket ping thread for this broker
        if broker_idx in self.ws_ping_threads:
            self.ws_ping_threads[broker_idx]['active'] = False

        # Mark this specific client as disconnected
        already_disconnected = False
        mqtt_info = None
        for info in self.mqtt_clients:
            if info['broker_idx'] == broker_idx:
                mqtt_info = info
                already_disconnected = not info.get('connected', False)
                info['connected'] = False
                info['connecting_since'] = 0
                info['reconnect_at'] = time.time() + self.reconnect_delay

                # If connection was short-lived (< 120 seconds), count it as a failure
                connect_time = info.get('connect_time', 0)
                if connect_time > 0 and (time.time() - connect_time) < 120:
                    info['failed_attempts'] = info.get('failed_attempts', 0) + 1
                    logger.warning(f"[{broker_name}] Short-lived connection detected (failed_attempts: {info['failed_attempts']})")
                elif connect_time > 0:
                    if info.get('failed_attempts', 0) > 0:
                        logger.info(f"[{broker_name}] Stable connection ended after {int(time.time() - connect_time)}s - resetting failure counter")
                        info['failed_attempts'] = 0

                break

        # Only log if this is the first disconnect event
        if not already_disconnected:
            logger.warning(f"[{broker_name}] Disconnected (code: {reason_code}, flags: {disconnect_flags}, properties: {properties})")

            # Track disconnect event for stats
            if mqtt_info and mqtt_info.get('connect_time', 0) > 0:
                current_time = time.time()
                if 'reconnects' not in self.stats:
                    self.stats['reconnects'] = {}
                if broker_idx not in self.stats['reconnects']:
                    self.stats['reconnects'][broker_idx] = []
                self.stats['reconnects'][broker_idx].append(current_time)

        # Check if ALL brokers are disconnected
        all_disconnected = all(not info.get('connected', False) for info in self.mqtt_clients)
        if all_disconnected:
            self.mqtt_connected = False

    def on_mqtt_message(self, client: mqtt.Client, userdata: dict[str, Any] | None, msg: Any) -> None:
        """Handle incoming MQTT messages (for remote serial commands)"""
        broker_idx = userdata.get('broker_idx', None) if userdata else None
        topic = msg.topic

        # Only handle serial command messages
        if '/serial/commands' not in topic:
            return

        broker = self._get_broker_config(broker_idx) if broker_idx is not None else {}
        broker_name = broker.get('name', f'broker-{broker_idx}')
        logger.debug(f"[{broker_name}] Received message on {topic}")

        try:
            jwt_token = msg.payload.decode('utf-8').strip()
            self._handle_serial_command(jwt_token, broker_idx)
        except Exception as e:
            logger.error(f"[SERIAL] Failed to handle command: {e}")

    def _handle_serial_command(self, jwt_token: str, broker_idx: int) -> None:
        """Process an incoming serial command JWT"""
        if not self.remote_serial_enabled:
            logger.warning("[SERIAL] Remote serial command received but feature is disabled")
            return

        if not self.remote_serial_allowed_companions:
            logger.warning("[SERIAL] Remote serial command received but no companions are allowed")
            return

        # First decode without verification to get the public key
        try:
            payload = decode_token_payload(jwt_token)
        except Exception as e:
            logger.warning(f"[SERIAL] Failed to decode command JWT: {e}")
            return

        # Extract and validate required fields
        companion_pubkey = payload.get('publicKey', '').upper()
        command = payload.get('command', '')
        target = payload.get('target', '').upper()
        nonce = payload.get('nonce', '')
        exp = payload.get('exp')
        iat = payload.get('iat')

        if not companion_pubkey or not command or not target or not nonce:
            logger.warning(f"[SERIAL] Missing required fields in command JWT")
            return

        # Verify target matches our public key
        if target != self.repeater_pub_key:
            logger.debug(f"[SERIAL] Command target {target[:8]}... doesn't match our key {self.repeater_pub_key[:8]}...")
            return

        # Verify companion is in allowlist
        if companion_pubkey not in self.remote_serial_allowed_companions:
            logger.warning(f"[SERIAL] Command from unauthorized companion: {companion_pubkey[:16]}...")
            self._publish_serial_response(command, nonce, False, "Unauthorized companion", broker_idx)
            return

        # Check expiry against our system clock
        current_time = int(time.time())
        if exp and current_time > exp:
            logger.warning(f"[SERIAL] Command JWT expired (exp={exp}, now={current_time})")
            self._publish_serial_response(command, nonce, False, "Command expired", broker_idx)
            return

        # Check nonce for replay protection
        self._cleanup_old_nonces()
        if nonce in self.remote_serial_nonces:
            logger.warning(f"[SERIAL] Duplicate nonce detected (replay attack?): {nonce[:16]}...")
            return  # Silently drop replays

        # Verify JWT signature using meshcore-decoder CLI
        try:
            verified_payload = verify_auth_token(jwt_token, companion_pubkey)
            logger.debug(f"[SERIAL] JWT signature verified for companion {companion_pubkey[:16]}...")
        except Exception as e:
            logger.warning(f"[SERIAL] JWT signature verification failed: {e}")
            self._publish_serial_response(command, nonce, False, "Invalid signature", broker_idx)
            return

        # Record nonce to prevent replay
        self.remote_serial_nonces[nonce] = current_time

        # Check if command is disallowed
        allowed, matched_rule = self._is_command_allowed(command)
        if not allowed:
            logger.warning(f"[SERIAL] Command blocked by rule '{matched_rule}': {command}")
            self._publish_serial_response(command, nonce, False, f"Command blocked: {matched_rule}", broker_idx)
            return

        # Execute the serial command
        logger.info(f"[SERIAL] Executing command from {companion_pubkey[:16]}...: {command}")
        success, response = self._execute_serial_command(command)

        # Publish response
        self._publish_serial_response(command, nonce, success, response, broker_idx)

    def _execute_serial_command(self, command: str) -> tuple[bool, str]:
        """
        Execute a serial command on the node and capture the response.
        Returns (success: bool, response: str)
        """
        if not self.ser:
            return False, "Serial port not connected"

        try:
            with self.ser_lock:
                # Flush buffers
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()

                # Send command (add newline if not present)
                cmd_bytes = command.strip()
                if not cmd_bytes.endswith('\r\n'):
                    cmd_bytes += '\r\n'

                self.ser.write(cmd_bytes.encode('utf-8'))
                logger.debug(f"[SERIAL] Sent: {command.strip()}")

                # Wait for response with timeout
                timeout = self.remote_serial_command_timeout
                start_time = time.time()
                response_lines = []

                while (time.time() - start_time) < timeout:
                    sleep(0.1)  # Small delay between reads

                    if self.ser.in_waiting > 0:
                        data = self.ser.read_all().decode(errors='replace')
                        response_lines.append(data)

                        # Check if we got a complete response (ends with prompt or newline)
                        full_response = ''.join(response_lines)
                        if '-> ' in full_response or full_response.rstrip().endswith('>'):
                            break

                # Parse response - extract the actual response after "-> "
                full_response = ''.join(response_lines)

                # Find the response content after "-> " or "> "
                if "-> >" in full_response:
                    response_text = full_response.split("-> >")[1].strip()
                elif "-> " in full_response:
                    response_text = full_response.split("-> ", 1)[1].strip()
                elif "> " in full_response:
                    response_text = full_response.split("> ", 1)[1].strip()
                else:
                    response_text = full_response.strip()

                # Clean up response (remove echo of command if present)
                if response_text.startswith(command.strip()):
                    response_text = response_text[len(command.strip()):].strip()

                # Remove trailing prompts
                response_text = response_text.rstrip('> ').strip()

                if not response_text:
                    response_text = "(no output)"

                logger.debug(f"[SERIAL] Response: {response_text[:100]}{'...' if len(response_text) > 100 else ''}")
                return True, response_text

        except serial.SerialException as e:
            logger.error(f"[SERIAL] Serial error executing command: {e}")
            return False, f"Serial error: {str(e)}"
        except Exception as e:
            logger.error(f"[SERIAL] Error executing command: {e}")
            return False, f"Error: {str(e)}"

    def _publish_serial_response(self, command: str, request_id: str, success: bool, response: str, broker_idx: int | None = None) -> None:
        """Create and publish a signed response JWT"""
        if not self.repeater_priv_key or not self.repeater_pub_key:
            logger.error("[SERIAL] Cannot sign response - private key not available")
            return

        try:
            # Create response JWT with claims
            claims = {
                'command': command,
                'request_id': request_id,
                'success': success,
                'response': response
            }

            # Create signed JWT using meshcore-decoder
            response_jwt = create_auth_token(
                self.repeater_pub_key,
                self.repeater_priv_key,
                expiry_seconds=60,  # Short expiry for responses
                **claims
            )

            # Publish to response topic
            response_topic = f"meshcore/{self.global_iata}/{self.repeater_pub_key}/serial/responses"

            # Publish to all connected brokers for redundancy
            published = False
            for mqtt_info in self.mqtt_clients:
                if mqtt_info.get('connected', False):
                    try:
                        broker = self._get_broker_config(mqtt_info['broker_idx'])
                        broker_name = broker.get('name', f"broker-{mqtt_info['broker_idx']}")
                        result = mqtt_info['client'].publish(response_topic, response_jwt, qos=1)
                        if result.rc == mqtt.MQTT_ERR_SUCCESS:
                            published = True
                            logger.debug(f"[{broker_name}] Published serial response to {response_topic}")
                    except Exception as e:
                        logger.error(f"[{broker_name}] Failed to publish serial response: {e}")

            if published:
                logger.info(f"[SERIAL] Response published (success={success}, request_id={request_id[:16]}...)")
            else:
                logger.error("[SERIAL] Failed to publish response to any broker")

        except Exception as e:
            logger.error(f"[SERIAL] Failed to create/publish response: {e}")

    def _cleanup_old_nonces(self) -> None:
        """Remove expired nonces from the tracking dict"""
        current_time = int(time.time())
        cutoff_time = current_time - self.remote_serial_nonce_ttl

        expired = [nonce for nonce, ts in self.remote_serial_nonces.items() if ts < cutoff_time]
        for nonce in expired:
            del self.remote_serial_nonces[nonce]

        if expired:
            logger.debug(f"[SERIAL] Cleaned up {len(expired)} expired nonces")

    def _subscribe_serial_commands(self, client: mqtt.Client, broker_idx: int) -> None:
        """Subscribe to the serial/commands topic for this node"""
        if not self.remote_serial_enabled:
            return

        if not self.repeater_pub_key:
            broker = self._get_broker_config(broker_idx)
            broker_name = broker.get('name', f'broker-{broker_idx}')
            logger.warning(f"[{broker_name}] Cannot subscribe to serial commands - public key not available")
            return

        # Topic: meshcore/{IATA}/{PUBLIC_KEY}/serial/commands
        topic = f"meshcore/{self.global_iata}/{self.repeater_pub_key}/serial/commands"

        broker = self._get_broker_config(broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')
        try:
            result = client.subscribe(topic, qos=1)
            if result[0] == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"[{broker_name}] Subscribed to remote serial: {topic}")
            else:
                logger.error(f"[{broker_name}] Failed to subscribe to {topic}: {mqtt.error_string(result[0])}")
        except Exception as e:
            logger.error(f"[{broker_name}] Error subscribing to {topic}: {e}")

    def build_status_message(self, status: str, include_stats: bool = True) -> dict[str, Any]:
        """Build a status message with all required fields"""
        message = {
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "origin": self.repeater_name,
            "origin_id": self.repeater_pub_key,
            "radio": self.radio_info if self.radio_info else "unknown",
            "model": self.model if self.model else "unknown",
            "firmware_version": self.firmware_version if self.firmware_version else "unknown",
            "client_version": self.client_version
        }

        # Add device stats if available and requested
        if include_stats and self.stats['device']:
            message['stats'] = self.stats['device']

        return message

    def publish_status(self, status: str, client: mqtt.Client | None = None, broker_idx: int | None = None) -> None:
        """Publish online status with stats (NOT retained)"""
        status_msg = self.build_status_message(status, include_stats=True)
        status_topic = self.get_topic("status", broker_idx)

        if client:
            self.safe_publish(status_topic, json.dumps(status_msg), retain=False, client=client, broker_idx=broker_idx)
        else:
            self.safe_publish(status_topic, json.dumps(status_msg), retain=False)

        logger.debug(f"Published status: {status}")

    def safe_publish(self, topic: str, payload: str, retain: bool = False, client: mqtt.Client | None = None, broker_idx: int | None = None) -> bool:
        """Publish to one or all MQTT brokers"""
        if not self.mqtt_connected:
            logger.warning(f"Not connected - skipping publish to {topic}")
            self.stats['publish_failures'] += 1
            return False

        success = False

        if client:
            clients_to_publish = [info for info in self.mqtt_clients if info['client'] == client]
        else:
            clients_to_publish = self.mqtt_clients

        for mqtt_client_info in clients_to_publish:
            bidx = mqtt_client_info['broker_idx']
            broker = self._get_broker_config(bidx)
            broker_name = broker.get('name', f'broker-{bidx}')
            try:
                mqtt_client = mqtt_client_info['client']
                qos = broker.get('qos', 0)
                if qos == 1:
                    qos = 0  # force qos=1 to 0 because qos 1 can cause retry storms

                result = mqtt_client.publish(topic, payload, qos=qos, retain=retain)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    logger.error(f"[{broker_name}] Publish failed to {topic}: {mqtt.error_string(result.rc)}")
                    self.stats['publish_failures'] += 1
                else:
                    logger.debug(f"[{broker_name}] Published to {topic}")
                    success = True
            except Exception as e:
                logger.error(f"[{broker_name}] Publish error to {topic}: {str(e)}")
                self.stats['publish_failures'] += 1

        return success

    def _create_mqtt_client(self, broker_idx: int) -> mqtt.Client | None:
        """
        Internal: Create and configure an MQTT client (doesn't connect).
        """
        broker = self._get_broker_config(broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')

        client_id = self.sanitize_client_id(self.repeater_pub_key)
        if broker_idx > 0:
            client_id += f"_{broker_idx}"

        transport = broker.get('transport', 'tcp')

        mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            transport=transport
        )

        mqtt_client.user_data_set({
            'name': broker_name,
            'broker_idx': broker_idx
        })

        # Set credentials
        username, password = self.generate_auth_credentials(broker_idx)
        if username is None:
            return None
        if username:
            mqtt_client.username_pw_set(username, password)

        # Set LWT
        lwt_topic = self.get_topic("status", broker_idx)
        lwt_payload = json.dumps(self.build_status_message("offline", include_stats=False))
        lwt_qos = broker.get('qos', 0)
        lwt_retain = broker.get('retain', True)
        mqtt_client.will_set(lwt_topic, lwt_payload, qos=lwt_qos, retain=lwt_retain)

        # Set callbacks
        mqtt_client.on_connect = self.on_mqtt_connect
        mqtt_client.on_disconnect = self.on_mqtt_disconnect
        mqtt_client.on_message = self.on_mqtt_message

        # Configure TLS
        tls_cfg = broker.get('tls', {})
        use_tls = tls_cfg.get('enabled', False)
        if use_tls:
            import ssl
            tls_verify = tls_cfg.get('verify', True)
            if tls_verify:
                mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                mqtt_client.tls_insecure_set(False)
            else:
                mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
                mqtt_client.tls_insecure_set(True)
                logger.warning(f"[{broker_name}] TLS verification disabled")

        # Configure WebSocket
        if transport == "websockets":
            mqtt_client.ws_set_options(path="/", headers=None)

        return mqtt_client

    def create_and_connect_broker(self, broker_idx: int) -> dict[str, Any] | None:
        """
        Create a fresh MQTT client and connect it.
        Returns client_info dict on success, None on failure.
        """
        if not self.repeater_name:
            logger.error("[MQTT] Cannot connect without repeater name")
            return None

        broker = self._get_broker_config(broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')

        if not broker.get('enabled', False):
            logger.debug(f"[{broker_name}] Disabled, skipping")
            return None

        server = broker.get('server', '')
        if not server:
            logger.error(f"[{broker_name}] No server configured")
            return None

        port = broker.get('port', 1883)
        transport = broker.get('transport', 'tcp')
        keepalive = broker.get('keepalive', 60)
        tls_cfg = broker.get('tls', {})
        use_tls = tls_cfg.get('enabled', False)

        logger.debug(f"[{broker_name}] Creating fresh client")

        # Create client
        mqtt_client = self._create_mqtt_client(broker_idx)
        if not mqtt_client:
            return None

        # Connect
        try:
            mqtt_client.connect(server, port, keepalive=keepalive)
            mqtt_client.loop_start()

            # Start WebSocket ping thread if needed
            if transport == "websockets":
                self.ws_ping_threads[broker_idx] = {'active': True}
                ping_thread = threading.Thread(
                    target=self._websocket_ping_loop,
                    args=(broker_idx, mqtt_client, transport),
                    daemon=True,
                    name=f"WS-Ping-{broker_name}"
                )
                ping_thread.start()

            logger.info(f"[{broker_name}] Connecting to {server}:{port} (transport={transport}, tls={use_tls}, keepalive={keepalive}s)")

            return {
                'client': mqtt_client,
                'broker_idx': broker_idx,
                'server': server,
                'port': port,
                'connected': False,
                'connecting_since': time.time(),
                'connect_time': 0,
                'reconnect_at': 0,
                'failed_attempts': 0
            }
        except Exception as e:
            logger.error(f"[{broker_name}] Failed to connect: {e}")
            return None

    def connect_mqtt(self) -> bool:
        """Initial connection to all configured MQTT brokers"""
        brokers = self.config.get('broker', [])
        logger.debug("=== MQTT Broker Configuration ===")
        for i, broker in enumerate(brokers):
            name = broker.get('name', f'broker-{i}')
            enabled = broker.get('enabled', False)
            if enabled:
                server = broker.get('server', 'unknown')
                port = broker.get('port', 1883)
                transport = broker.get('transport', 'tcp')
                tls_cfg = broker.get('tls', {})
                use_tls = tls_cfg.get('enabled', False)
                auth = broker.get('auth', {})
                auth_method = auth.get('method', 'none')
                logger.debug(f"  [{name}] ENABLED - {server}:{port} (transport={transport}, tls={use_tls}, auth={auth_method})")
            else:
                logger.debug(f"  [{name}] DISABLED")
        logger.debug("=================================")

        # Connect to all enabled brokers
        for i, broker in enumerate(brokers):
            self.connection_events[i] = threading.Event()

            client_info = self.create_and_connect_broker(i)
            if client_info:
                self.mqtt_clients.append(client_info)

        if len(self.mqtt_clients) == 0:
            logger.error("[MQTT] Failed to connect to any broker")
            return False

        logger.info(f"[MQTT] Initiated connection to {len(self.mqtt_clients)} broker(s)")

        # Wait for all brokers to complete initial connection attempt
        max_wait = 10  # seconds per broker
        for mqtt_info in self.mqtt_clients:
            broker_idx = mqtt_info['broker_idx']
            event = self.connection_events.get(broker_idx)
            if event:
                event.wait(timeout=max_wait)

        # Check if at least one connected
        if not self.mqtt_connected:
            logger.error("[MQTT] No brokers connected after initial connection attempts")
            return False

        return True

    def _stop_websocket_ping_thread(self, broker_idx: int) -> None:
        """Cleanly stop the WebSocket ping thread for a broker"""
        if broker_idx in self.ws_ping_threads:
            self.ws_ping_threads[broker_idx]['active'] = False
            # Give thread a moment to exit cleanly
            time.sleep(0.1)
            # Remove from dict to prevent memory leak
            del self.ws_ping_threads[broker_idx]
            broker = self._get_broker_config(broker_idx)
            logger.debug(f"[{broker.get('name', broker_idx)}] Stopped WebSocket ping thread")

    def reconnect_disconnected_brokers(self) -> None:
        """
        Check for disconnected brokers and recreate them.
        Exit after max_reconnect_attempts consecutive failures per broker.
        """
        current_time = time.time()

        for i, mqtt_info in enumerate(self.mqtt_clients):
            # Skip if already connected
            if mqtt_info.get('connected', False):
                continue

            # Skip if currently connecting (but only if it's been < 10 seconds)
            connecting_since = mqtt_info.get('connecting_since', 0)
            if connecting_since > 0 and (current_time - connecting_since) < 10:
                continue

            # Check if it's time to attempt reconnect
            if current_time < mqtt_info.get('reconnect_at', 0):
                continue

            broker_idx = mqtt_info['broker_idx']
            broker = self._get_broker_config(broker_idx)
            broker_name = broker.get('name', f'broker-{broker_idx}')
            failed_attempts = mqtt_info.get('failed_attempts', 0)

            # Exit if too many failures
            if failed_attempts >= self.max_reconnect_attempts:
                logger.critical(f"[{broker_name}] {self.max_reconnect_attempts} consecutive failures - exiting for service restart")
                sys.exit(1)

            logger.info(f"[{broker_name}] Reconnecting (attempt #{failed_attempts + 1})")

            # Stop old client cleanly
            old_client = mqtt_info.get('client')
            if old_client:
                try:
                    self._stop_websocket_ping_thread(broker_idx)
                    old_client.loop_stop()
                    old_client.disconnect()
                except Exception as e:
                    logger.debug(f"[{broker_name}] Error stopping old client: {e}")

            # Clear token cache to force fresh token
            if broker_idx in self.token_cache:
                del self.token_cache[broker_idx]

            # Create fresh client
            new_client_info = self.create_and_connect_broker(broker_idx)

            if new_client_info:
                self.mqtt_clients[i] = new_client_info
                logger.debug(f"[{broker_name}] Recreated client successfully")
            else:
                mqtt_info['failed_attempts'] = failed_attempts + 1
                jitter = random.uniform(-0.5, 0.5)
                delay = max(0, self.reconnect_delay + jitter)
                mqtt_info['reconnect_at'] = current_time + delay
                self.reconnect_delay = min(self.reconnect_delay * self.reconnect_backoff, self.max_reconnect_delay)
                logger.warning(f"[{broker_name}] Failed to recreate client (attempt #{failed_attempts + 1}/{self.max_reconnect_attempts})")

    def parse_and_publish(self, line: str) -> None:
        if not line:
            return
        logger.debug(f"From Radio: {line}")
        message = {
            "origin": self.repeater_name,
            "origin_id": self.repeater_pub_key,
            "timestamp": datetime.now().isoformat()
        }

        # Handle RAW messages
        if "U RAW:" in line:
            parts = line.split("U RAW:")
            if len(parts) > 1:
                raw_hex = parts[1].strip()
                self.last_raw = raw_hex
                # Count actual bytes (hex string is 2x the actual byte count)
                self.stats['bytes_processed'] += len(raw_hex) // 2

        # Handle DEBUG messages
        if self.debug:
            if line.startswith("DEBUG"):
                message.update({
                    "type": "DEBUG",
                    "message": line
                })
                debug_topic = self.get_topic("debug")
                if debug_topic:
                    self.safe_publish(debug_topic, json.dumps(message))
                return

        # Handle Packet messages (RX and TX)
        packet_match = PACKET_PATTERN.match(line)
        if packet_match:
            direction = packet_match.group(3).lower()  # rx or tx

            # Update packet counters
            if direction == "rx":
                self.stats['packets_rx'] += 1
            else:
                self.stats['packets_tx'] += 1

            packet_type = packet_match.group(5)
            payload = {
                "type": "PACKET",
                "direction": direction,
                "time": packet_match.group(1),
                "date": packet_match.group(2),
                "len": packet_match.group(4),
                "packet_type": packet_type,
                "route": packet_match.group(6),
                "payload_len": packet_match.group(7),
                "raw": self.last_raw
            }

            # Add SNR, RSSI, score, and hash for RX packets
            if direction == "rx":
                payload.update({
                    "SNR": packet_match.group(8),
                    "RSSI": packet_match.group(9),
                    "score": packet_match.group(10),
                    "duration": packet_match.group(12),
                    "hash": packet_match.group(13)
                })

                # Add path for route=D
                if packet_match.group(6) == "D" and packet_match.group(14):
                    payload["path"] = packet_match.group(14)

            message.update(payload)
            packets_topic = self.get_topic("packets")
            if packets_topic:
                self.safe_publish(packets_topic, json.dumps(message))
            return

    def handle_signal(self, signum: int, frame: Any) -> None:
        """Signal handler to trigger graceful shutdown."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.should_exit = True

    def wait_for_system_time_sync(self) -> bool:
        """
        Wait up to 60 seconds for system clock synchronization via timedatectl.

        Always returns True to allow the caller to proceed. If timedatectl is
        unavailable (e.g., non-systemd systems) or any error occurs, logs a
        warning and returns immediately. Respects should_exit for clean shutdown.
        """
        attempts = 0
        while attempts < 60 and not self.should_exit:
            try:
                result = subprocess.run(
                    ['timedatectl', 'status'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
            except FileNotFoundError:
                logger.warning("timedatectl not found — skipping sync check and continuing.")
                return True  # Don't loop 60 times
            except Exception as e:
                logger.warning("Error checking time sync (%s). Continuing.", e)
                return True

            if "System clock synchronized: yes" in result.stdout:
                return True
            logger.warning("System clock is not synchronized: %s",
                           result.stderr.strip() or result.stdout.strip())
            attempts += 1
            time.sleep(1)

        logger.warning("Timed out waiting for system clock sync — continuing anyway.")
        return True


    def run(self) -> None:
        log_config_sources(self.config)

        if not self.connect_serial():
            return

        if self.sync_time_at_start:
            self.wait_for_system_time_sync()
            self.set_repeater_time()

        if not self.get_repeater_name():
            logger.error("Failed to get repeater name")
            return

        if not self.get_repeater_pubkey():
            logger.error("Failed to get the repeater id (public key)")
            return

        if not self.get_repeater_privkey():
            logger.warning("Failed to get repeater private key - auth token authentication will not be available")

        # Get radio info before connecting to MQTT
        self.radio_info = self.get_radio_info()
        if not self.radio_info:
            logger.error("Failed to get radio info")
            return

        # Get firmware version
        self.firmware_version = self.get_firmware_version()
        if not self.firmware_version:
            logger.warning("Failed to get firmware version - will continue without it")

        # Get board type
        self.model = self.get_board_type()
        if not self.model:
            logger.warning("Failed to get board type - will continue without it")

        # Get initial device stats
        device_stats = self.get_device_stats()
        if device_stats:
            self.stats['device'] = device_stats
            self.stats['device_prev'] = device_stats.copy()
            logger.info(f"Device stats: {device_stats}")
        else:
            logger.debug("Device stats not available (firmware may not support stats commands)")

        # Log client version
        logger.info(f"Client version: {self.client_version}")

        # Log remote serial configuration
        if self.remote_serial_enabled:
            if self.remote_serial_allowed_companions:
                logger.info(f"Remote serial: ENABLED ({len(self.remote_serial_allowed_companions)} companion(s) allowed)")
                for pubkey in sorted(self.remote_serial_allowed_companions):
                    logger.debug(f"  Allowed companion: {pubkey[:16]}...")
            else:
                logger.warning("Remote serial: ENABLED but no companions configured (will reject all commands)")
            if self.remote_serial_disallowed_commands:
                logger.info(f"Remote serial blocked commands: {self.remote_serial_disallowed_commands}")
        else:
            logger.info("Remote serial: DISABLED")

        # Initial MQTT connection
        retry_count = 0
        max_initial_retries = 10
        while retry_count < max_initial_retries:
            if self.connect_mqtt():
                break
            else:
                retry_count += 1
                wait_time = min(retry_count * 2, 30)  # Max 30 seconds between initial retries
                logger.warning(f"[MQTT] Initial connection failed. Retrying in {wait_time}s... (attempt {retry_count}/{max_initial_retries})")
                sleep(wait_time)

        if retry_count >= max_initial_retries:
            logger.error("[MQTT] Failed to establish initial connection after maximum retries")
            sys.exit(1)

        # Start stats logging thread
        stats_thread = threading.Thread(
            target=self._stats_logging_loop,
            daemon=True,
            name="Stats-Logger"
        )
        stats_thread.start()
        logger.debug("[STATS] Started statistics logging thread")

        try:
            while True:
                if self.should_exit:
                    break

                # Check and reconnect any disconnected brokers
                self.reconnect_disconnected_brokers()

                try:
                    # Check for serial data (with lock for thread safety)
                    with self.ser_lock:
                        if self.ser and self.ser.in_waiting > 0:
                            line = self.ser.readline().decode(errors='replace').strip()
                            logger.debug(f"RX: {line}")
                            self.parse_and_publish(line)
                except OSError:
                    logger.warning("Serial connection unavailable, trying to reconnect")
                    self.close_serial()
                    self.connect_serial()
                    sleep(0.5)
                sleep(0.01)

        except KeyboardInterrupt:
            logger.info("\nExiting...")
        except Exception as e:
            logger.exception(f"Unhandled error in main loop: {e}")
        finally:
            # Cleanup MQTT clients
            for mqtt_client_info in self.mqtt_clients:
                try:
                    mqtt_client_info['client'].loop_stop()
                    mqtt_client_info['client'].disconnect()
                except:
                    pass

            # Close serial connection
            self.close_serial()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument("--config", action="append", default=None, help="Path to TOML config file (can be specified multiple times; overrides default config loading)")
    args: argparse.Namespace = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    # Load config
    config = load_config(args.config)

    # Reconfigure log level from config
    log_level_str = config.get('general', {}).get('log_level', 'INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    if args.debug:
        log_level = logging.DEBUG
    logger.setLevel(log_level)
    logging.getLogger().setLevel(log_level)

    bridge = MeshCoreBridge(config, debug=args.debug)

    # Ensure signals from systemd (SIGTERM) and ctrl-c (SIGINT) are handled
    signal.signal(signal.SIGTERM, bridge.handle_signal)
    signal.signal(signal.SIGINT, bridge.handle_signal)

    bridge.run()
