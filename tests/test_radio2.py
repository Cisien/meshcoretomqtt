"""Tests for second radio profile (radio2) reporting: packet log and status."""
from __future__ import annotations

import json

from bridge.message_parser import PACKET_RADIO_PATTERN, parse_and_publish
from bridge.mqtt_publish import build_status_message
from bridge.radio_profile import RadioId, RadioMode, RadioProfile
from tests.fakes import FakeBrokerClient, FakeSerialConnection, make_test_state

RAW = "12:34:56 - 29/10/2026 U RAW: 15A3"
RADIO2 = "12:34:56 - 29/10/2026 U RADIO: radio2"
RX = ("12:34:56 - 29/10/2026 U: RX, len=64 (type=4, route=F, payload_len=48) "
      "SNR=9 RSSI=-71 score=1000 time=88 hash=ABCD1234")
TX = "12:34:57 - 29/10/2026 U: TX, len=65 (type=4, route=F, payload_len=48)"


def _state():
    broker = FakeBrokerClient()
    broker._connected = True
    state = make_test_state(
        broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}],
        repeater_name="TestNode",
        repeater_pub_key="AA" * 32,
    )
    return state, broker


def _packets(broker):
    return [json.loads(payload) for topic, payload, _, _ in broker.published if topic.endswith("/packets")]


class TestPacketRadioPattern:
    def test_matches_radio_line(self):
        match = PACKET_RADIO_PATTERN.match(RADIO2)
        assert match is not None
        assert match.group("radio_id") == RadioId.SECOND
        assert match.group("time") == "12:34:56"
        assert match.group("date") == "29/10/2026"

    def test_ignores_other_lines(self):
        assert PACKET_RADIO_PATTERN.match(RAW) is None
        assert PACKET_RADIO_PATTERN.match(RX) is None

    def test_ignores_unknown_radio_ids(self):
        assert PACKET_RADIO_PATTERN.match("12:34:56 - 29/10/2026 U RADIO: radio9") is None


class TestPacketTagging:
    def test_rx_on_radio2_is_tagged(self):
        state, broker = _state()
        for line in (RAW, RADIO2, RX):
            parse_and_publish(state, line)
        (packet,) = _packets(broker)
        assert packet["rx_radio_id"] == "radio2"
        assert packet["mediamesh"] is True
        assert "tx_radio_id" not in packet
        assert packet["raw"] == "15A3"

    def test_tx_on_radio2_is_tagged(self):
        state, broker = _state()
        for line in (RAW, RADIO2, TX):
            parse_and_publish(state, line)
        (packet,) = _packets(broker)
        assert packet["tx_radio_id"] == "radio2"
        assert packet["mediamesh"] is True
        assert "rx_radio_id" not in packet

    def test_main_radio_packet_is_unchanged(self):
        state, broker = _state()
        for line in (RAW, RX):
            parse_and_publish(state, line)
        (packet,) = _packets(broker)
        assert "rx_radio_id" not in packet
        assert "mediamesh" not in packet

    def test_tag_applies_to_one_packet_only(self):
        state, broker = _state()
        for line in (RAW, RADIO2, RX, RAW, RX):
            parse_and_publish(state, line)
        first, second = _packets(broker)
        assert first["rx_radio_id"] == "radio2"
        assert "rx_radio_id" not in second

    def test_new_raw_line_drops_stale_tag(self):
        state, broker = _state()
        for line in (RAW, RADIO2, RAW, RX):
            parse_and_publish(state, line)
        (packet,) = _packets(broker)
        assert "rx_radio_id" not in packet

    def test_radio_line_is_not_published(self):
        state, broker = _state()
        parse_and_publish(state, RADIO2)
        assert broker.published == []


class TestStatusMessage:
    def test_radios_added_when_radio2_on(self):
        profile = RadioProfile(radio="911.150,250.000,7,5", mode=RadioMode.RXTX, temporary=False)
        state = make_test_state(device=FakeSerialConnection(radio2_profile=profile))
        state.radio2_profile = state.device.get_radio2_profile()
        message = build_status_message(state, "online")
        assert message["radios"] == [{
            "id": "radio2", "radio": "911.150,250.000,7,5", "mode": "rxtx",
            "temporary": False, "mediamesh": True}]

    def test_no_radios_without_radio2(self):
        state = make_test_state(device=FakeSerialConnection())
        state.radio2_profile = state.device.get_radio2_profile()
        assert "radios" not in build_status_message(state, "online")
