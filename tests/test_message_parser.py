"""Tests for message parsing and MQTT publishing."""
from __future__ import annotations

import json

import pytest

from bridge.message_parser import RAW_PATTERN, PACKET_PATTERN, parse_and_publish
from tests.fakes import FakeBrokerClient, make_test_state


class TestRawPattern:
    def test_matches_raw_line(self):
        line = "12:34:56 - 1/15/2025 U RAW: AABB0011"
        match = RAW_PATTERN.match(line)
        assert match is not None
        assert match.group(3) == "AABB0011"


class TestPacketPattern:
    def test_matches_rx_packet(self):
        line = "12:34:56 - 1/15/2025 U: RX, len=64 (type=1, route=D, payload_len=48) SNR=10 RSSI=-80 score=100 hash=ABCD1234"
        match = PACKET_PATTERN.match(line)
        assert match is not None
        assert match.group(3) == "RX"
        assert match.group(4) == "64"
        assert match.group(5) == "1"
        assert match.group(6) == "D"
        assert match.group(8) == "10"
        assert match.group(9) == "-80"
        assert match.group(10) == "100"

    def test_matches_tx_packet(self):
        line = "12:34:56 - 1/15/2025 U: TX, len=32 (type=2, route=F, payload_len=16)"
        match = PACKET_PATTERN.match(line)
        assert match is not None
        assert match.group(3) == "TX"


class TestParseAndPublish:
    def _make_state(self):
        broker = FakeBrokerClient()
        broker._connected = True
        state = make_test_state(
            broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}],
            repeater_name="TestNode",
            repeater_pub_key="AA" * 32,
        )
        return state, broker

    def test_rx_packet(self):
        state, broker = self._make_state()
        line = "12:34:56 - 1/15/2025 U: RX, len=64 (type=1, route=D, payload_len=48) SNR=10 RSSI=-80 score=100 hash=ABCD1234"
        parse_and_publish(state, line)
        assert len(broker.published) == 1
        msg = json.loads(broker.published[0][1])
        assert msg['type'] == "PACKET"
        assert msg['direction'] == "rx"
        assert state.stats['packets_rx'] == 1

    def test_tx_packet(self):
        state, broker = self._make_state()
        line = "12:34:56 - 1/15/2025 U: TX, len=32 (type=2, route=F, payload_len=16)"
        parse_and_publish(state, line)
        assert len(broker.published) == 1
        msg = json.loads(broker.published[0][1])
        assert msg['direction'] == "tx"
        assert state.stats['packets_tx'] == 1

    def test_raw_updates_bytes(self):
        state, broker = self._make_state()
        line = "12:34:56 - 1/15/2025 U RAW: AABB0011CCDD"
        parse_and_publish(state, line)
        # 12 hex chars = 6 bytes
        assert state.stats['bytes_processed'] == 6
        assert state.last_raw == "AABB0011CCDD"

    def test_debug_mode(self):
        state, broker = self._make_state()
        state.debug = True
        line = "DEBUG some debug info"
        parse_and_publish(state, line)
        assert len(broker.published) == 1
        msg = json.loads(broker.published[0][1])
        assert msg['type'] == "DEBUG"

    def test_ignores_junk(self):
        state, broker = self._make_state()
        parse_and_publish(state, "random garbage line")
        assert len(broker.published) == 0

    def test_empty_line(self):
        state, broker = self._make_state()
        parse_and_publish(state, "")
        assert len(broker.published) == 0

    def test_extracts_snr_rssi(self):
        state, broker = self._make_state()
        line = "12:34:56 - 1/15/2025 U: RX, len=64 (type=1, route=D, payload_len=48) SNR=-5 RSSI=-100 score=50 hash=ABCD1234"
        parse_and_publish(state, line)
        msg = json.loads(broker.published[0][1])
        assert msg['SNR'] == "-5"
        assert msg['RSSI'] == "-100"
        assert msg['score'] == "50"
