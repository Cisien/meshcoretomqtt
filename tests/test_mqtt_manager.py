"""Tests for MqttManager orchestration."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

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
            "reconnect_delay": 1.0,
            "failed_attempts": 0,
        }]

        # Reconnect will try to create a new client, which may fail since
        # there's no real MQTT server, but it should clear the token cache
        manager.reconnect_disconnected_brokers()
        assert 0 not in state.token_cache

    def test_backoff_is_independent_per_broker(self):
        """Two brokers maintain separate reconnect_delay counters.

        Broker 0 fails repeatedly so its backoff grows.
        Broker 1 stays connected throughout, then disconnects.
        Broker 1's reconnect_delay must still be 1.0 — unaffected by broker 0.
        When broker 1 subsequently fails its first reconnect attempt its own
        backoff starts at 1.0 and grows independently from broker 0's.
        """
        config = make_config()
        config['broker'] = [
            {'name': 'b0', 'enabled': True, 'server': 'host0', 'port': 1883,
             'transport': 'tcp', 'qos': 0, 'retain': False,
             'auth': {'method': 'none'}, 'tls': {'enabled': False}},
            {'name': 'b1', 'enabled': True, 'server': 'host1', 'port': 1883,
             'transport': 'tcp', 'qos': 0, 'retain': False,
             'auth': {'method': 'none'}, 'tls': {'enabled': False}},
        ]
        state = make_test_state(config=config, repeater_name="Node", repeater_pub_key="AA" * 32)
        manager = MqttManager(state)
        state.mqtt_manager = manager

        def make_client_info(idx, connected):
            return {
                'client': FakeBrokerClient(),
                'broker_idx': idx,
                'connected': connected,
                'connecting_since': 0,
                'connect_time': time.time() - 300 if connected else 0,
                'reconnect_at': 0,
                'reconnect_delay': 1.0,
                'failed_attempts': 0,
            }

        state.mqtt_clients = [make_client_info(0, False), make_client_info(1, True)]
        state.mqtt_connected = True

        # Fail broker 0 five times — its delay should grow: 1.0 → 1.5 → 2.25 → 3.375 → 5.06 → 7.59
        with patch.object(manager, '_create_and_connect_broker', return_value=None):
            for _ in range(5):
                state.mqtt_clients[0]['reconnect_at'] = 0
                manager.reconnect_disconnected_brokers()

        assert state.mqtt_clients[0]['reconnect_delay'] == pytest.approx(1.0 * 1.5 ** 5, rel=1e-3)
        assert state.mqtt_clients[1]['reconnect_delay'] == 1.0, \
            "broker 1 delay must be unaffected by broker 0 failures"

        # Broker 1 disconnects — reconnect_at should use its own delay (1.0)
        t_before = time.time()
        manager.on_mqtt_disconnect(None, {'name': 'b1', 'broker_idx': 1}, None, 0, None)
        t_after = time.time()

        assert state.mqtt_clients[1]['reconnect_delay'] == 1.0
        assert t_before + 1.0 <= state.mqtt_clients[1]['reconnect_at'] <= t_after + 1.0

        # Broker 1 fails its first reconnect — its own backoff starts from 1.0
        with patch.object(manager, '_create_and_connect_broker', return_value=None):
            state.mqtt_clients[1]['reconnect_at'] = 0
            manager.reconnect_disconnected_brokers()

        assert state.mqtt_clients[1]['reconnect_delay'] == pytest.approx(1.5, rel=1e-3), \
            "broker 1 first failure must grow from 1.0, not from broker 0's grown value"


class TestInitialConnectFailure:
    def _make_manager(self):
        config = make_config()
        config['broker'] = [
            {'name': 'b0', 'enabled': True, 'server': 'host0', 'port': 1883,
             'transport': 'tcp', 'qos': 0, 'retain': False,
             'auth': {'method': 'none'}, 'tls': {'enabled': False}},
            {'name': 'b1', 'enabled': True, 'server': 'host1', 'port': 1883,
             'transport': 'tcp', 'qos': 0, 'retain': False,
             'auth': {'method': 'none'}, 'tls': {'enabled': False}},
        ]
        state = make_test_state(config=config, repeater_name="Node", repeater_pub_key="AA" * 32)
        manager = MqttManager(state)
        state.mqtt_manager = manager
        return state, manager

    @staticmethod
    def _ok_info(idx):
        return {
            'client': FakeBrokerClient(), 'broker_idx': idx, 'connected': True,
            'connecting_since': 0, 'connect_time': time.time(), 'reconnect_at': 0,
            'reconnect_delay': 1.0, 'failed_attempts': 0,
        }

    def _connect(self, state, manager):
        # broker 0 fails its initial connect, broker 1 succeeds
        results = {0: None, 1: self._ok_info(1)}
        state.mqtt_connected = True
        with patch.object(manager, '_create_and_connect_broker', side_effect=lambda i: results[i]),                 patch.object(threading.Event, 'wait', return_value=True):
            return manager.connect_all_brokers()

    def test_failed_initial_connect_stays_tracked(self):
        state, manager = self._make_manager()
        assert self._connect(state, manager) is True

        assert [i['broker_idx'] for i in state.mqtt_clients] == [0, 1]
        placeholder = state.mqtt_clients[0]
        assert placeholder['client'] is None
        assert placeholder['connected'] is False
        assert placeholder['failed_attempts'] == 1
        assert placeholder['reconnect_at'] > 0

    def test_failed_initial_connect_is_retried(self):
        state, manager = self._make_manager()
        self._connect(state, manager)
        state.mqtt_clients[0]['reconnect_at'] = 0

        recovered = self._ok_info(0)
        recovered['connected'] = False
        with patch.object(manager, '_create_and_connect_broker', return_value=recovered) as create:
            manager.reconnect_disconnected_brokers()

        create.assert_called_once_with(0)
        assert state.mqtt_clients[0] is recovered

    def test_failed_initial_connect_uses_backoff_and_attempt_limit(self):
        state, manager = self._make_manager()
        self._connect(state, manager)
        state.mqtt_clients[0]['reconnect_at'] = 0

        with patch.object(manager, '_create_and_connect_broker', return_value=None):
            manager.reconnect_disconnected_brokers()
        assert state.mqtt_clients[0]['failed_attempts'] == 2
        assert state.mqtt_clients[0]['reconnect_delay'] == pytest.approx(state.reconnect_backoff)

        state.mqtt_clients[0]['failed_attempts'] = state.max_reconnect_attempts
        state.mqtt_clients[0]['reconnect_at'] = 0
        manager.reconnect_disconnected_brokers()
        assert state.should_exit is True

    def test_status_total_counts_failed_broker(self):
        state, manager = self._make_manager()
        self._connect(state, manager)
        total = len(state.mqtt_clients)
        connected = sum(1 for info in state.mqtt_clients if info.get('connected', False))
        assert (connected, total) == (1, 2)

    def test_no_client_created_returns_false(self):
        state, manager = self._make_manager()
        with patch.object(manager, '_create_and_connect_broker', return_value=None):
            assert manager.connect_all_brokers() is False

    def test_retrying_connect_all_does_not_duplicate_entries(self):
        state, manager = self._make_manager()
        with patch.object(manager, '_create_and_connect_broker', return_value=None):
            manager.connect_all_brokers()
            manager.connect_all_brokers()
        assert len(state.mqtt_clients) == 2
