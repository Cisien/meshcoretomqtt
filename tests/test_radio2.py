"""Tests for second radio profile (radio2) reporting."""
from __future__ import annotations

import json

from bridge.message_parser import parse_and_publish
from bridge.mqtt_publish import build_status_message
from bridge.radio2 import parse_radio2_reply
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


class TestRadioLine:
    def test_sets_pending_radio_id(self):
        state, _ = _state()
        parse_and_publish(state, RADIO2)
        assert state.pending_radio_id == "radio2"

    def test_other_lines_do_not(self):
        state, _ = _state()
        for line in (RAW, RX):
            parse_and_publish(state, line)
        assert state.pending_radio_id is None


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

    def test_other_radio_id_is_tagged_without_mediamesh(self):
        state, broker = _state()
        for line in (RAW, "12:34:56 - 29/10/2026 U RADIO: radio3", RX):
            parse_and_publish(state, line)
        (packet,) = _packets(broker)
        assert packet["rx_radio_id"] == "radio3"
        assert "mediamesh" not in packet

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


class TestParseRadio2Reply:
    def test_rxtx(self):
        assert parse_radio2_reply("get radio2\r\n  -> > 911.150,250.000,7,5,rxtx,32 (auto)\r\n") == {
            "radio": "911.150,250.000,7,5", "mode": "rxtx", "temporary": False}

    def test_real_firmware_suffixes(self):
        # Replies seen from mikecarper/MeshCore v1.18.0 on a Wio Tracker L1 Pro
        for reply in (
            "get radio2\r\n  -> > 911.150,250.000,7,5,rxtx,40 (auto); timing self-test pending; wait=113s\r\n",
            "get radio2\r\n  -> > 911.150,250.000,7,5,rxtx,40 (auto); WARN recommended preamble: radio2=40; switch=638us\r\n",
        ):
            assert parse_radio2_reply(reply) == {
                "radio": "911.150,250.000,7,5", "mode": "rxtx", "temporary": False}

    def test_rx_temporary(self):
        reply = "  -> > 911.150,250.000,7,5,rx,32; 0d2h0m (120 min) left"
        assert parse_radio2_reply(reply) == {
            "radio": "911.150,250.000,7,5", "mode": "rx", "temporary": True}

    def test_off(self):
        assert parse_radio2_reply("  -> > off") is None

    def test_unsupported_firmware(self):
        assert parse_radio2_reply("  -> Unknown command") is None
        assert parse_radio2_reply("") is None
        assert parse_radio2_reply(None) is None


class TestStatusMessage:
    def test_radios_added_when_radio2_on(self):
        info = {"radio": "911.150,250.000,7,5", "mode": "rxtx", "temporary": False}
        state = make_test_state(device=FakeSerialConnection(radio2_info=info))
        state.radio2_info = state.device.get_radio2_info()
        message = build_status_message(state, "online")
        assert message["radios"] == [{
            "id": "radio2", "radio": "911.150,250.000,7,5", "mode": "rxtx",
            "temporary": False, "mediamesh": True}]

    def test_no_radios_without_radio2(self):
        state = make_test_state(device=FakeSerialConnection())
        state.radio2_info = state.device.get_radio2_info()
        assert "radios" not in build_status_message(state, "online")
