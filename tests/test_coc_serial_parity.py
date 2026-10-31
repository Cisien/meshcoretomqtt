"""Compare real local serial and CoC IPC inputs through the same MQTT pipeline.

Uses a pseudo-terminal, socket pairs and an in-memory broker, never hardware,
BlueZ, or a network broker. Only the host processing timestamp is normalized.
"""
import json
import os
import pty
import socket
import time
import uuid

import pytest
import serial

from bridge.coc_connection import CocConnection, decode_packet, encode_packet, recv_frame, send_frame
from bridge.runner import process_device_line
from bridge.serial_connection import RealSerialConnection
from tests.fakes import FakeBrokerClient, make_test_state


def pipeline(device):
    broker = FakeBrokerClient()
    state = make_test_state(
        device=device, repeater_name="ParityNode", repeater_pub_key="AB" * 32,
        broker_clients=[{"client": broker, "connected": True, "broker_idx": 0}],
    )
    return state, broker


def publications(broker):
    result = []
    for topic, text, qos, retain in broker.published:
        payload = json.loads(text)
        assert payload.pop("timestamp")  # Host processing time, not radio event time.
        result.append((topic, payload, qos, retain))
    return result


@pytest.mark.parametrize("raw_bytes,route", [(4, "D"), (255, "F")])
def test_serial_and_coc_produce_identical_packet_publications(raw_bytes, route):
    raw = bytes(index % 256 for index in range(raw_bytes)).hex().upper()
    prefix = "12:34:56 - 28/10/2026 U"
    path = " [AA -> BB]" if route == "D" else ""
    lines = [
        f"{prefix} RAW: {raw}\r\n",
        f"{prefix}: RX, len={raw_bytes} (type=1, route={route}, payload_len=2) "
        f"SNR=-7 RSSI=-103 score=850 time=25 hash=AB12{path}\n",
        f"{prefix}: TX, len=4 (type=2, route=F, payload_len=2)\r\n",
        f"{prefix}: TX FAIL!, len=4 (type=2, route=F, payload_len=2)\n",
    ]
    assert all(len(line.encode()) <= 544 for line in lines)
    master, slave = pty.openpty()
    port = serial.Serial(os.ttyname(slave), 115200, timeout=.1)
    usb = RealSerialConnection(port)
    data, radio_data = socket.socketpair()
    control, radio_control = socket.socketpair()
    radio_data.settimeout(2)
    radio_control.settimeout(2)
    coc = CocConnection.from_sockets(
        data, control, 123, peer="parity-" + uuid.uuid4().hex,
        peer_properties={key: True for key in ("Paired", "Trusted", "Bonded", "encrypted")},
    )
    try:
        for channel in (radio_data, radio_control):
            send_frame(channel, encode_packet(1, 123, 0, b"\x20\x02"))
        coc.start()
        for channel in (radio_data, radio_control):
            assert decode_packet(recv_frame(channel), "device").kind == 2
        usb_state, usb_broker = pipeline(usb)
        coc_state, coc_broker = pipeline(coc)
        for ident, line in enumerate(lines, 1):
            os.write(master, line.encode())
            send_frame(radio_data, encode_packet(3, 123, ident, line.encode()))
            receipt = decode_packet(recv_frame(radio_control), "device")
            assert receipt.kind == 4 and receipt.ident == ident
            for state in (usb_state, coc_state):
                deadline = time.monotonic() + 2
                while not process_device_line(state):
                    assert time.monotonic() < deadline
                    time.sleep(.002)
        assert publications(coc_broker) == publications(usb_broker)
        assert len(coc_broker.published) == 2
        payload = publications(coc_broker)[0][1]
        assert payload["raw"] == raw
        assert (payload["time"], payload["date"]) == ("12:34:56", "28/10/2026")
        assert (payload["SNR"], payload["RSSI"], payload["duration"], payload["hash"]) == ("-7", "-103", "25", "AB12")
        assert ("path" in payload) == (route == "D")
        for key in ("bytes_processed", "packets_rx", "packets_tx", "publish_failures"):
            assert coc_state.stats[key] == usb_state.stats[key]
    finally:
        coc.close()
        usb.close()
        radio_data.close()
        radio_control.close()
        os.close(master)
        os.close(slave)


def test_stats_mapping_matches_serial_for_same_cli_json():
    # Recorded synthetic CLI values: this checks mapping, not a live radio.
    responses = {
        "stats-core": {"battery_mv": 4100, "uptime_secs": 123, "errors": 0, "queue_len": 2},
        "stats-radio": {"noise_floor": -110, "tx_air_secs": 3, "rx_air_secs": 4},
        "stats-packets": {"recv_errors": 1},
    }

    class SerialReplies(RealSerialConnection):
        def _send_unlocked(self, command, delay=.5):
            return "-> " + json.dumps(responses[command.strip()]) + "\r\n"

    class CocReplies(CocConnection):
        def __init__(self):
            pass

        def _get(self, command):
            return self._extract_response(command, json.dumps(responses[command]))

    assert CocReplies().get_device_stats() == SerialReplies(None).get_device_stats() == {
        "battery_mv": 4100, "uptime_secs": 123, "debug_flags": 0, "queue_len": 2,
        "noise_floor": -110, "tx_air_secs": 3, "rx_air_secs": 4, "recv_errors": 1,
    }
