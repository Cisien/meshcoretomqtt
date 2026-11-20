"""Second radio profile (radio2) reply parsing."""
from __future__ import annotations

import re
from typing import Any

# "911.150,250.000,7,5,rxtx,32 (auto)" -> freq, bw, sf, cr, mode, rest
RADIO2_REPLY_PATTERN = re.compile(
    r"^\s*([0-9.]+),([0-9.]+),(\d+),(\d+),(rxtx|rx)\b(.*)$"
)


def parse_radio2_reply(response: str | None) -> dict[str, Any] | None:
    """Parse a `get radio2` reply into {radio, mode, temporary}, or None.

    The reply looks like "-> > 911.150,250.000,7,5,rxtx,32 (auto)", with
    "; 0d2h0m (120 min) left" appended while a temporary profile runs, or
    "-> > off". Firmware without a second radio profile answers with an error.
    """
    if not response or "-> >" not in response:
        return None
    reply = response.split("-> >", 1)[1].strip().split('\n')[0].strip()
    match = RADIO2_REPLY_PATTERN.match(reply)
    if not match:
        return None
    freq, bw, sf, cr, mode, rest = match.groups()
    return {
        "radio": f"{freq},{bw},{sf},{cr}",
        "mode": mode,
        "temporary": " left" in rest,
    }
