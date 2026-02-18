"""Tests for MeshCoreBridge facade."""
from __future__ import annotations

from bridge import MeshCoreBridge
from bridge.state import BridgeState
from bridge.mqtt_manager import MqttManager
from tests.fakes import make_config


class TestMeshCoreBridgeFacade:
    def test_creates_state(self):
        config = make_config()
        bridge = MeshCoreBridge(config, debug=True, version="1.0.8.0")
        assert isinstance(bridge.state, BridgeState)
        assert bridge.state.debug is True

    def test_creates_mqtt_manager(self):
        config = make_config()
        bridge = MeshCoreBridge(config, version="1.0.8.0")
        assert isinstance(bridge.state.mqtt_manager, MqttManager)

    def test_client_version_set(self):
        config = make_config()
        bridge = MeshCoreBridge(config, version="1.0.8.0")
        assert bridge.state.client_version.startswith("meshcoretomqtt/1.0.8.0")

    def test_handle_signal_sets_exit(self):
        config = make_config()
        bridge = MeshCoreBridge(config, version="1.0.8.0")
        assert bridge.state.should_exit is False
        bridge.handle_signal(15, None)
        assert bridge.state.should_exit is True
