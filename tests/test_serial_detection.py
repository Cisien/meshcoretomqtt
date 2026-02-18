"""Tier 5: Tests for serial device detection with a real plugged-in device.

Requires MCTOMQTT_TEST_SERIAL_DEVICE env var pointing to a real device.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from installer.system import detect_serial_devices

pytestmark = pytest.mark.e2e


@pytest.fixture()
def serial_device() -> str:
    device = os.environ.get("MCTOMQTT_TEST_SERIAL_DEVICE")
    if not device:
        pytest.skip("Set MCTOMQTT_TEST_SERIAL_DEVICE=/dev/ttyACM0 to run")
    return device


class TestDetectSerialDevices:
    def test_detects_env_device(self, serial_device: str) -> None:
        devices = detect_serial_devices()
        # The device or its symlink target should be in the list
        serial_resolved = str(Path(serial_device).resolve())
        found = False
        for d in devices:
            if d == serial_device or str(Path(d).resolve()) == serial_resolved:
                found = True
                break
        assert found, f"{serial_device} not in detected devices: {devices}"

    def test_at_least_one_char_device(self, serial_device: str) -> None:
        devices = detect_serial_devices()
        assert len(devices) > 0
        # At least one should be a real character device
        has_char = any(Path(d).resolve().is_char_device() for d in devices)
        assert has_char, f"No character devices in: {devices}"
