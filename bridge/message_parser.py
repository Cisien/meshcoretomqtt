"""Serial line parsing and MQTT publishing."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import topics
from .mqtt_publish import safe_publish

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)

RAW_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U RAW: (.*)")
PACKET_PATTERN = re.compile(
    r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U: (RX|TX), len=(\d+) \(type=(\d+), route=([A-Z]), payload_len=(\d+)\)"
    r"(?: SNR=(-?\d+) RSSI=(-?\d+) score=(\d+)(?: time=(\d+))?)?"
    r"(?: hash=([0-9A-F]+))?"
    r"(?: \[(.*)\])?$"
)
# Firmware with a second radio profile (e.g. Keymind Cascade radio2) prints this
# between the RAW and RX/TX lines of a packet heard or sent on that profile.
RADIO_PATTERN = re.compile(r"\d{2}:\d{2}:\d{2} - \d{1,2}/\d{1,2}/\d{4} U RADIO: (radio\d+)\s*$")


def parse_and_publish(state: BridgeState, line: str) -> None:
    """Parse a serial line and publish to MQTT."""
    if not line:
        return

    logger.debug(f"From Radio: {line}")

    message: dict = {
        "origin": state.repeater_name,
        "origin_id": state.repeater_pub_key,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # Handle RAW messages
    if "U RAW:" in line:
        parts = line.split("U RAW:")
        if len(parts) > 1:
            raw_hex = parts[1].strip()
            state.last_raw = raw_hex
            # A new packet starts here; its RADIO line, if any, follows.
            state.pending_radio_id = None
            state.stats['bytes_processed'] += len(raw_hex) // 2

    # Handle DEBUG messages
    if state.debug:
        if line.startswith("DEBUG"):
            message.update({
                "type": "DEBUG",
                "message": line
            })
            safe_publish(state, "debug", json.dumps(message))
            return

    # Remember which radio the next RX/TX line belongs to
    radio_match = RADIO_PATTERN.match(line)
    if radio_match:
        state.pending_radio_id = radio_match.group(1)
        return

    # Handle Packet messages (RX and TX)
    packet_match = PACKET_PATTERN.match(line)
    if packet_match:
        direction = packet_match.group(3).lower()

        if direction == "rx":
            state.stats['packets_rx'] += 1
        else:
            state.stats['packets_tx'] += 1

        payload: dict = {
            "type": "PACKET",
            "direction": direction,
            "time": packet_match.group(1),
            "date": packet_match.group(2),
            "len": packet_match.group(4),
            "packet_type": packet_match.group(5),
            "route": packet_match.group(6),
            "payload_len": packet_match.group(7),
            "raw": state.last_raw
        }

        if direction == "rx":
            payload.update({
                "SNR": packet_match.group(8),
                "RSSI": packet_match.group(9),
                "score": packet_match.group(10),
                "duration": packet_match.group(11),
                "hash": packet_match.group(12)
            })

            if packet_match.group(6) == "D" and packet_match.group(13):
                payload["path"] = packet_match.group(13)

        # Packet heard or sent on a second radio profile; see RADIO_PATTERN.
        if state.pending_radio_id:
            radio_id = state.pending_radio_id
            payload["rx_radio_id" if direction == "rx" else "tx_radio_id"] = radio_id
            if radio_id == "radio2":
                payload["mediamesh"] = True
            state.pending_radio_id = None

        message.update(payload)
        safe_publish(state, "packets", json.dumps(message))
