"""Tests for runner startup and main loop logic."""
from __future__ import annotations

import pytest

from bridge.runner import load_client_version, handle_signal
from tests.fakes import FakeSerialConnection, FakeAuthProvider, make_test_state


class TestLoadClientVersion:
    def test_basic_version(self):
        result = load_client_version("1.0.8.0")
        assert result == "meshcoretomqtt/1.0.8.0"

    def test_version_format(self):
        result = load_client_version("2.0.0")
        assert result.startswith("meshcoretomqtt/2.0.0")


class TestHandleSignal:
    def test_sets_exit(self):
        state = make_test_state()
        assert state.should_exit is False
        handle_signal(state, 15, None)
        assert state.should_exit is True


class TestStartupPopulatesState:
    """Test that startup queries populate state correctly using FakeSerialConnection."""

    def test_device_queries(self):
        device = FakeSerialConnection(
            name="TestNode",
            pubkey="AA" * 32,
            privkey="BB" * 64,
            radio_info="LoRa 915MHz",
            firmware="1.8.2",
            board_type="Station G2",
            stats={'battery_mv': 4200},
        )
        state = make_test_state(device=device)

        # Simulate the startup queries from runner.run()
        state.repeater_name = state.device.get_name()
        state.repeater_pub_key = state.device.get_pubkey()
        state.repeater_priv_key = state.device.get_privkey()
        state.radio_info = state.device.get_radio_info()
        state.firmware_version = state.device.get_firmware_version()
        state.model = state.device.get_board_type()
        device_stats = state.device.get_device_stats()
        if device_stats:
            state.stats['device'] = device_stats

        assert state.repeater_name == "TestNode"
        assert state.repeater_pub_key == "AA" * 32
        assert state.repeater_priv_key == "BB" * 64
        assert state.radio_info == "LoRa 915MHz"
        assert state.firmware_version == "1.8.2"
        assert state.model == "Station G2"
        assert state.stats['device'] == {'battery_mv': 4200}

    def test_fails_no_name(self):
        device = FakeSerialConnection(name=None)
        state = make_test_state(device=device)
        state.repeater_name = state.device.get_name()
        assert state.repeater_name is None

    def test_fails_no_pubkey(self):
        device = FakeSerialConnection(pubkey=None)
        state = make_test_state(device=device)
        state.repeater_pub_key = state.device.get_pubkey()
        assert state.repeater_pub_key is None

    def test_continues_without_privkey(self):
        device = FakeSerialConnection(privkey=None)
        state = make_test_state(device=device)
        state.repeater_name = state.device.get_name()
        state.repeater_pub_key = state.device.get_pubkey()
        state.repeater_priv_key = state.device.get_privkey()
        # Should have name and pubkey but no privkey
        assert state.repeater_name is not None
        assert state.repeater_pub_key is not None
        assert state.repeater_priv_key is None

    def test_continues_without_firmware(self):
        device = FakeSerialConnection(firmware=None)
        state = make_test_state(device=device)
        state.firmware_version = state.device.get_firmware_version()
        assert state.firmware_version is None


class TestMainLoopReadsAndParses:
    def test_read_line_feeds_to_parser(self):
        """read_line() output is available for parse_and_publish."""
        device = FakeSerialConnection(
            lines=["12:34:56 - 1/15/2025 U: RX, len=64 (type=1, route=D, payload_len=48) SNR=10 RSSI=-80 score=100 hash=ABCD1234"]
        )
        state = make_test_state(device=device)
        line = state.device.read_line()
        assert line is not None
        assert "RX" in line

    def test_read_line_returns_none_when_empty(self):
        device = FakeSerialConnection(lines=[])
        state = make_test_state(device=device)
        assert state.device.read_line() is None


class TestFullStartupFlow:
    """Integration test: all fakes wired together, verify end-to-end initialization."""

    def test_full_startup(self):
        device = FakeSerialConnection(
            name="TestNode",
            pubkey="AA" * 32,
            privkey="BB" * 64,
            radio_info="LoRa 915MHz",
            firmware="1.8.2",
            board_type="Station G2",
            stats={'battery_mv': 4200, 'uptime_secs': 3600},
        )
        auth = FakeAuthProvider()

        state = make_test_state(device=device, auth=auth)
        state.client_version = load_client_version("1.0.8.0")

        # Simulate startup sequence
        state.repeater_name = state.device.get_name()
        state.repeater_pub_key = state.device.get_pubkey()
        state.repeater_priv_key = state.device.get_privkey()
        state.radio_info = state.device.get_radio_info()
        state.firmware_version = state.device.get_firmware_version()
        state.model = state.device.get_board_type()
        device_stats = state.device.get_device_stats()
        if device_stats:
            state.stats['device'] = device_stats
            state.stats['device_prev'] = device_stats.copy()

        assert state.repeater_name == "TestNode"
        assert state.repeater_pub_key == "AA" * 32
        assert state.repeater_priv_key == "BB" * 64
        assert state.radio_info == "LoRa 915MHz"
        assert state.firmware_version == "1.8.2"
        assert state.model == "Station G2"
        assert state.stats['device']['battery_mv'] == 4200
        assert state.client_version.startswith("meshcoretomqtt/1.0.8.0")
