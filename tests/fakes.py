"""Shared fake implementations for bridge tests."""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from bridge.serial_connection import SerialConnection
from bridge.auth_provider import AuthProvider
from bridge.broker_client import BrokerClient
from bridge.state import BridgeState


class FakeSerialConnection(SerialConnection):
    """Configurable fake — set return values per-method."""

    def __init__(
        self,
        *,
        name: str | None = "TestNode",
        pubkey: str | None = "AA" * 32,
        privkey: str | None = "BB" * 64,
        radio_info: str | None = "LoRa 915MHz",
        firmware: str | None = "1.8.2",
        board_type: str | None = "Station G2",
        stats: dict[str, Any] | None = None,
        command_responses: dict[str, tuple[bool, str]] | None = None,
        lines: list[str] | None = None,
    ) -> None:
        self.name = name
        self.pubkey = pubkey
        self.privkey = privkey
        self.radio_info = radio_info
        self.firmware = firmware
        self.board_type = board_type
        self._stats = stats or {}
        self._command_responses = command_responses or {}
        self._lines = list(lines) if lines else []
        self._closed = False
        self.time_set = False
        self.commands_executed: list[str] = []
        self._last_activity = time.time()

    def set_time(self) -> None:
        self.time_set = True

    def get_name(self) -> str | None:
        return self.name

    def get_pubkey(self) -> str | None:
        return self.pubkey

    def get_privkey(self) -> str | None:
        return self.privkey

    def get_radio_info(self) -> str | None:
        return self.radio_info

    def get_firmware_version(self) -> str | None:
        return self.firmware

    def get_board_type(self) -> str | None:
        return self.board_type

    def get_device_stats(self) -> dict[str, Any]:
        return self._stats

    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]:
        self.commands_executed.append(command)
        if command in self._command_responses:
            return self._command_responses[command]
        return True, "(no output)"

    def read_line(self) -> str | None:
        if self._lines:
            self._last_activity = time.time()
            return self._lines.pop(0)
        return None

    def seconds_since_activity(self) -> float:
        return time.time() - self._last_activity

    def close(self) -> None:
        self._closed = True

    @property
    def is_open(self) -> bool:
        return not self._closed


class FakeAuthProvider(AuthProvider):
    """Returns deterministic tokens, configurable verify behavior."""

    def __init__(self, *, valid_keys: set[str] | None = None, should_fail_verify: bool = False) -> None:
        self._valid_keys = valid_keys or set()
        self._should_fail_verify = should_fail_verify

    def create_token(self, public_key_hex: str, private_key_hex: str,
                     expiry_seconds: int = 3600, **claims: Any) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"EdDSA"}').rstrip(b'=').decode()
        payload_data = {"publicKey": public_key_hex, "exp": int(time.time()) + expiry_seconds}
        payload_data.update(claims)
        payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b'=').decode()
        return f"{header}.{payload}.fakesig"

    def verify_token(self, token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]:
        if self._should_fail_verify:
            raise Exception("Verification failed")
        if expected_public_key_hex and expected_public_key_hex not in self._valid_keys:
            raise Exception("Verification failed")
        return self.decode_payload(token)

    def decode_payload(self, token: str) -> dict[str, Any]:
        parts = token.split('.')
        if len(parts) != 3:
            raise Exception(f"Invalid token format: expected 3 parts, got {len(parts)}")
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))


class FakeBrokerClient(BrokerClient):
    """Records all publish/subscribe calls for assertion."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscribed: list[str] = []
        self._connected = False
        self.connect_calls: list[tuple[str, int, int]] = []
        self.disconnect_calls: int = 0

    def connect(self, server: str, port: int, keepalive: int = 60) -> None:
        self.connect_calls.append((server, port, keepalive))
        self._connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool:
        self.published.append((topic, payload, qos, retain))
        return True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return self._connected


def make_config(**overrides: Any) -> dict[str, Any]:
    """Factory for minimal valid TOML config dict."""
    config: dict[str, Any] = {
        'general': {'iata': 'TST', 'sync_time': False},
        'serial': {'ports': ['/dev/ttyACM0'], 'baud_rate': 115200, 'timeout': 2},
        'topics': {
            'packets': 'meshcore/{IATA}/{PUBLIC_KEY}/packets',
            'status': 'meshcore/{IATA}/{PUBLIC_KEY}/status',
            'debug': 'meshcore/{IATA}/{PUBLIC_KEY}/debug',
        },
        'broker': [{
            'name': 'test-broker',
            'enabled': True,
            'server': 'localhost',
            'port': 1883,
            'transport': 'tcp',
            'qos': 0,
            'retain': True,
            'auth': {'method': 'none'},
            'tls': {'enabled': False},
        }],
        'remote_serial': {'enabled': False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in config and isinstance(config[key], dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def make_test_state(
    *,
    config: dict[str, Any] | None = None,
    device: SerialConnection | None = None,
    auth: AuthProvider | None = None,
    broker_clients: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> BridgeState:
    """Factory for BridgeState with sensible defaults + optional fake injection."""
    if config is None:
        config = make_config()
    state = BridgeState(config, debug=overrides.pop('debug', False))
    if device is not None:
        state.device = device
    if auth is not None:
        state.auth = auth
    if broker_clients is not None:
        state.mqtt_clients = broker_clients
        state.mqtt_connected = any(info.get('connected', False) for info in broker_clients)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state
