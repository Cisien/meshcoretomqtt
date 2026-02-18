"""Tests for BrokerClient and safe_publish."""
from __future__ import annotations

import json

import pytest

from bridge.mqtt_publish import safe_publish, build_status_message, publish_status
from tests.fakes import FakeBrokerClient, make_test_state, make_config


class TestFakeBrokerClient:
    def test_records_publish(self):
        client = FakeBrokerClient()
        result = client.publish("test/topic", '{"msg":"hello"}', qos=0, retain=True)
        assert result is True
        assert len(client.published) == 1
        assert client.published[0] == ("test/topic", '{"msg":"hello"}', 0, True)

    def test_tracks_subscriptions(self):
        client = FakeBrokerClient()
        client.subscribe("test/topic")
        client.subscribe("other/topic")
        assert client.subscribed == ["test/topic", "other/topic"]

    def test_connect_disconnect(self):
        client = FakeBrokerClient()
        assert not client.is_connected
        client.connect("localhost", 1883)
        assert client.is_connected
        client.disconnect()
        assert not client.is_connected


class TestSafePublish:
    def test_sends_to_all_connected(self):
        broker1 = FakeBrokerClient()
        broker1._connected = True
        broker2 = FakeBrokerClient()
        broker2._connected = True
        state = make_test_state(
            broker_clients=[
                {"client": broker1, "broker_idx": 0, "connected": True},
                {"client": broker2, "broker_idx": 1, "connected": True},
            ],
            repeater_pub_key="AA" * 32,
        )
        result = safe_publish(state, "test/topic", '{"msg":"hello"}')
        assert result is True
        assert len(broker1.published) == 1
        assert len(broker2.published) == 1

    def test_skips_when_not_connected(self):
        state = make_test_state()
        state.mqtt_connected = False
        result = safe_publish(state, "test/topic", '{"msg":"hello"}')
        assert result is False
        assert state.stats['publish_failures'] == 1

    def test_single_broker_via_client(self):
        broker1 = FakeBrokerClient()
        broker1._connected = True
        broker2 = FakeBrokerClient()
        broker2._connected = True
        state = make_test_state(
            broker_clients=[
                {"client": broker1, "broker_idx": 0, "connected": True},
                {"client": broker2, "broker_idx": 1, "connected": True},
            ],
            repeater_pub_key="AA" * 32,
        )
        result = safe_publish(state, "test/topic", '{"msg":"hello"}', client=broker1)
        assert result is True
        assert len(broker1.published) == 1
        assert len(broker2.published) == 0

    def test_forces_qos_zero(self):
        """QoS 1 in config should be forced to 0."""
        broker = FakeBrokerClient()
        broker._connected = True
        config = make_config()
        config['broker'][0]['qos'] = 1
        state = make_test_state(
            config=config,
            broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}],
            repeater_pub_key="AA" * 32,
        )
        safe_publish(state, "test/topic", '{"msg":"hello"}')
        assert broker.published[0][2] == 0  # qos forced to 0


class TestBuildStatusMessage:
    def test_online(self):
        state = make_test_state(
            repeater_name="TestNode",
            repeater_pub_key="AA" * 32,
            radio_info="LoRa 915MHz",
            model="Station G2",
            firmware_version="1.8.2",
            client_version="meshcoretomqtt/1.0.8.0",
        )
        msg = build_status_message(state, "online")
        assert msg['status'] == "online"
        assert msg['origin'] == "TestNode"
        assert msg['origin_id'] == "AA" * 32
        assert msg['radio'] == "LoRa 915MHz"
        assert msg['model'] == "Station G2"
        assert msg['firmware_version'] == "1.8.2"

    def test_offline_without_stats(self):
        state = make_test_state(repeater_name="TestNode", repeater_pub_key="AA" * 32)
        msg = build_status_message(state, "offline", include_stats=False)
        assert msg['status'] == "offline"
        assert 'stats' not in msg

    def test_includes_device_stats(self):
        state = make_test_state(repeater_name="TestNode", repeater_pub_key="AA" * 32)
        state.stats['device'] = {'battery_mv': 4200}
        msg = build_status_message(state, "online", include_stats=True)
        assert msg['stats'] == {'battery_mv': 4200}


class TestPublishStatus:
    def test_uses_correct_topic(self):
        broker = FakeBrokerClient()
        broker._connected = True
        state = make_test_state(
            broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}],
            repeater_name="TestNode",
            repeater_pub_key="AA" * 32,
        )
        publish_status(state, "online")
        assert len(broker.published) == 1
        topic = broker.published[0][0]
        assert "status" in topic
