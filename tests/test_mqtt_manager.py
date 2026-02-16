"""Tests for MqttManager orchestration."""
from __future__ import annotations

import pytest

from bridge.mqtt_manager import MqttManager
from tests.fakes import FakeBrokerClient, make_test_state, make_config


class TestMqttManagerCallbacks:
    def _make_manager(self, **state_kwargs):
        state = make_test_state(
            repeater_name="TestNode",
            repeater_pub_key="AA" * 32,
            **state_kwargs,
        )
        manager = MqttManager(state)
        state.mqtt_manager = manager
        return state, manager

    def test_on_connect_sets_connected(self):
        state, manager = self._make_manager()
        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": False, "connecting_since": 1, "connect_time": 0, "failed_attempts": 0}]

        import threading
        state.connection_events[0] = threading.Event()

        manager.on_mqtt_connect(None, {'name': 'test', 'broker_idx': 0}, None, 0)

        assert state.mqtt_clients[0]['connected'] is True
        assert state.mqtt_connected is True

    def test_on_connect_rejects_nonzero_rc(self):
        state, manager = self._make_manager()
        import threading
        state.connection_events[0] = threading.Event()

        manager.on_mqtt_connect(None, {'name': 'test', 'broker_idx': 0}, None, 1)
        assert state.mqtt_connected is False

    def test_on_disconnect_marks_disconnected(self):
        state, manager = self._make_manager()
        state.mqtt_clients = [{"client": FakeBrokerClient(), "broker_idx": 0, "connected": True, "connecting_since": 0, "connect_time": 100, "reconnect_at": 0, "failed_attempts": 0}]
        state.mqtt_connected = True

        manager.on_mqtt_disconnect(None, {'name': 'test', 'broker_idx': 0}, None, 0, None)

        assert state.mqtt_clients[0]['connected'] is False
        assert state.mqtt_connected is False

    def test_on_message_ignores_non_serial(self):
        state, manager = self._make_manager()
        msg = type('Msg', (), {'topic': 'other/topic', 'payload': b'test'})()
        # Should not raise
        manager.on_mqtt_message(None, {'name': 'test', 'broker_idx': 0}, msg)


class TestReconnectBehavior:
    def test_skips_connected_brokers(self):
        state = make_test_state(repeater_name="TestNode", repeater_pub_key="AA" * 32)
        manager = MqttManager(state)
        state.mqtt_manager = manager

        broker = FakeBrokerClient()
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True, "connecting_since": 0, "connect_time": 100, "reconnect_at": 0, "failed_attempts": 0}]

        # Should not attempt reconnect for connected broker
        manager.reconnect_disconnected_brokers()
        assert state.mqtt_clients[0]['connected'] is True

    def test_clears_token_cache_on_reconnect(self):
        config = make_config()
        state = make_test_state(
            config=config,
            repeater_name="TestNode",
            repeater_pub_key="AA" * 32,
        )
        manager = MqttManager(state)
        state.mqtt_manager = manager

        state.token_cache[0] = ("cached_token", 1000)

        broker = FakeBrokerClient()
        state.mqtt_clients = [{
            "client": broker,
            "broker_idx": 0,
            "connected": False,
            "connecting_since": 0,
            "connect_time": 100,
            "reconnect_at": 0,
            "failed_attempts": 0,
        }]

        # Reconnect will try to create a new client, which may fail since
        # there's no real MQTT server, but it should clear the token cache
        manager.reconnect_disconnected_brokers()
        assert 0 not in state.token_cache
