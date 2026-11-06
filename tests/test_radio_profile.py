"""Tests for bridge/radio_profile.py: the second radio profile (radio2)."""
from __future__ import annotations

from bridge.radio_profile import (
    RadioId,
    RadioMode,
    RadioProfile,
    packet_radio_tags,
    parse_get_radio2_reply,
)

MEDIAMESH = RadioProfile(radio="911.150,250.000,7,5", mode=RadioMode.RXTX, temporary=False)


class TestParseGetRadio2Reply:
    def test_rxtx(self):
        reply = "get radio2\r\n  -> > 911.150,250.000,7,5,rxtx,32 (auto)\r\n"
        assert parse_get_radio2_reply(reply) == MEDIAMESH

    def test_rx_temporary(self):
        reply = "  -> > 911.150,250.000,7,5,rx,32; 0d2h0m (120 min) left"
        assert parse_get_radio2_reply(reply) == RadioProfile(
            radio="911.150,250.000,7,5", mode=RadioMode.RX, temporary=True)

    def test_off(self):
        assert parse_get_radio2_reply("  -> > off") is None

    def test_unknown_mode(self):
        assert parse_get_radio2_reply("  -> > 911.150,250.000,7,5,tx,32") is None

    def test_unsupported_firmware(self):
        assert parse_get_radio2_reply("  -> Unknown command") is None
        assert parse_get_radio2_reply("") is None
        assert parse_get_radio2_reply(None) is None


class TestStatusEntry:
    def test_status_entry(self):
        assert MEDIAMESH.status_entry() == {
            "id": "radio2", "radio": "911.150,250.000,7,5", "mode": "rxtx",
            "temporary": False, "mediamesh": True}


class TestPacketRadioTags:
    def test_received_on_second_radio(self):
        assert packet_radio_tags("rx", RadioId.SECOND) == {"rx_radio_id": "radio2", "mediamesh": True}

    def test_sent_on_second_radio(self):
        assert packet_radio_tags("tx", RadioId.SECOND) == {"tx_radio_id": "radio2", "mediamesh": True}

    def test_main_radio(self):
        assert packet_radio_tags("rx", None) == {}
        assert packet_radio_tags("tx", None) == {}
