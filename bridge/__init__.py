"""MeshCore to MQTT bridge package."""
from __future__ import annotations

from typing import Any

from .state import BridgeState
from .mqtt_manager import MqttManager
from . import runner


class MeshCoreBridge:
    """Facade: creates BridgeState, wires up MqttManager, exposes run() and handle_signal()."""

    def __init__(self, config: dict[str, Any], debug: bool = False, version: str = "0.0.0") -> None:
        self.state = BridgeState(config, debug)
        self.state.client_version = runner.load_client_version(version)
        self.state.mqtt_manager = MqttManager(self.state)

    def run(self) -> None:
        runner.run(self.state)

    def handle_signal(self, signum: int, frame: Any) -> None:
        runner.handle_signal(self.state, signum, frame)
