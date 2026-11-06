"""Second radio profile (radio2): what the device reports about it, and how
packets on it are tagged.

Firmware such as Keymind Cascade can listen on a second radio profile
(`set radio2 ...`) alongside the main one, the profile MediaMesh uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RadioId(StrEnum):
    """Radio ids the firmware names in its packet log (`U RADIO: <id>`).

    Only the second radio is ever named; a packet without a RADIO line was
    heard or sent on the main radio.
    """

    SECOND = "radio2"


class RadioMode(StrEnum):
    """How the second radio profile is used: listen only, or listen and send."""

    RX = "rx"
    RXTX = "rxtx"


@dataclass(frozen=True)
class RadioProfile:
    """The second radio profile as `get radio2` reports it."""

    radio: str  # "MHz,kHz,SF,CR", the same shape as `get radio`
    mode: RadioMode
    temporary: bool  # a time-limited profile (tempradio2) is running

    def status_entry(self) -> dict[str, Any]:
        """The status message's `radios` entry for this profile."""
        return {
            "id": RadioId.SECOND.value,
            "radio": self.radio,
            "mode": self.mode.value,
            "temporary": self.temporary,
            "mediamesh": True,
        }


GET_RADIO2_REPLY_PATTERN = re.compile(
    r"^\s*(?P<freq_mhz>[0-9.]+),(?P<bandwidth_khz>[0-9.]+)"
    r",(?P<spreading_factor>\d+),(?P<coding_rate>\d+)"
    rf",(?P<mode>{'|'.join(map(re.escape, RadioMode))})\b"
    r"(?P<details>.*)$"
)
TEMPORARY_PROFILE_SUFFIX = " left"  # "; 0d2h0m (120 min) left"


def parse_get_radio2_reply(response: str | None) -> RadioProfile | None:
    """Read a `get radio2` reply.

    For example "-> > 911.150,250.000,7,5,rxtx,32 (auto)", which ends in
    "; 0d2h0m (120 min) left" while a temporary profile runs. "-> > off", an
    error from firmware without a second profile, or anything else gives None.
    """
    if not response or "-> >" not in response:
        return None
    reply = response.split("-> >", 1)[1].strip().split("\n")[0].strip()
    match = GET_RADIO2_REPLY_PATTERN.match(reply)
    if not match:
        return None
    return RadioProfile(
        radio=",".join(match.group("freq_mhz", "bandwidth_khz", "spreading_factor", "coding_rate")),
        mode=RadioMode(match.group("mode")),
        temporary=TEMPORARY_PROFILE_SUFFIX in match.group("details"),
    )


def packet_radio_tags(direction: str, radio_id: RadioId | None) -> dict[str, Any]:
    """Keys added to a packet message for the radio that carried it.

    `direction` is "rx" or "tx". These are the keys the firmware's own MQTT
    client publishes, so analyzers read both the same way.
    """
    match radio_id:
        case RadioId.SECOND:
            return {f"{direction}_radio_id": radio_id.value, "mediamesh": True}
        case _:
            return {}
