"""Serial line parsing and MQTT publishing."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from . import topics
from .mqtt_publish import safe_publish

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)

RAW_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U RAW: (.*)")
PACKET_PATTERN = re.compile(
    r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U: (RX|TX), len=(\d+) \(type=(\d+), route=([A-Z]), payload_len=(\d+)\)"
    r"(?: SNR=(-?\d+) RSSI=(-?\d+) score=(\d+)( time=(\d+))? hash=([0-9A-F]+)(?: \[(.*)\])?)?"
)


def parse_and_publish(state: BridgeState, line: str) -> None:
    """Parse a serial line and publish to MQTT."""
    if not line:
        return

    logger.debug(f"From Radio: {line}")

    message: dict = {
        "origin": state.repeater_name,
        "origin_id": state.repeater_pub_key,
        "timestamp": datetime.now().isoformat()
    }

    # Handle RAW messages
    if "U RAW:" in line:
        parts = line.split("U RAW:")
        if len(parts) > 1:
            raw_hex = parts[1].strip()
            state.last_raw = raw_hex
            state.stats['bytes_processed'] += len(raw_hex) // 2

    # Handle DEBUG messages
    if state.debug:
        if line.startswith("DEBUG"):
            message.update({
                "type": "DEBUG",
                "message": line
            })
            debug_topic = topics.get_topic(state, "debug")
            if debug_topic:
                safe_publish(state, debug_topic, json.dumps(message))
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
                "duration": packet_match.group(12),
                "hash": packet_match.group(13)
            })

            if packet_match.group(6) == "D" and packet_match.group(14):
                payload["path"] = packet_match.group(14)

        message.update(payload)
        packets_topic = topics.get_topic(state, "packets")
        if packets_topic:
            safe_publish(state, packets_topic, json.dumps(message))
