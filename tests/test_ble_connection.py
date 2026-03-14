"""Tests for BLESerialConnection — pure logic, no BLE hardware required."""
from __future__ import annotations

import queue
import time

from bridge.ble_connection import BLESerialConnection


def _make_conn(ble_cfg: dict | None = None) -> BLESerialConnection:
    """Create a BLESerialConnection without starting the BLE background thread."""
    config = {'ble': ble_cfg or {}}
    conn = BLESerialConnection.__new__(BLESerialConnection)
    conn._config = config
    conn._lines = queue.Queue()
    conn._partial = ""
    conn._last_activity = time.time()
    conn._should_stop = False
    conn._ble_connected = False
    return conn


# ------------------------------------------------------------------
# _on_notification line-buffering
# ------------------------------------------------------------------

class TestOnNotification:
    def test_single_complete_line(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"hello\n"))
        assert conn.read_line() == "hello"

    def test_strips_carriage_return(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"hello\r\n"))
        assert conn.read_line() == "hello"

    def test_partial_then_complete(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"hel"))
        assert conn.read_line() is None
        conn._on_notification(None, bytearray(b"lo\n"))
        assert conn.read_line() == "hello"

    def test_multiple_lines_in_one_notification(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"line1\nline2\n"))
        assert conn.read_line() == "line1"
        assert conn.read_line() == "line2"
        assert conn.read_line() is None

    def test_chunked_across_three_notifications(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"abc"))
        conn._on_notification(None, bytearray(b"def"))
        conn._on_notification(None, bytearray(b"ghi\n"))
        assert conn.read_line() == "abcdefghi"

    def test_empty_line_not_queued(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"\n"))
        assert conn.read_line() is None

    def test_updates_last_activity(self):
        conn = _make_conn()
        conn._last_activity = time.time() - 100
        conn._on_notification(None, bytearray(b"data\n"))
        assert conn.seconds_since_activity() < 1

    def test_does_not_update_activity_on_empty_line(self):
        conn = _make_conn()
        conn._last_activity = time.time() - 100
        conn._on_notification(None, bytearray(b"\n"))
        assert conn.seconds_since_activity() >= 99

    def test_invalid_utf8_replaced(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"\xff\xfe\n"))
        line = conn.read_line()
        assert line is not None
        assert '\n' not in line

    def test_trailing_partial_held(self):
        conn = _make_conn()
        conn._on_notification(None, bytearray(b"complete\npartial"))
        assert conn.read_line() == "complete"
        assert conn.read_line() is None
        assert conn._partial == "partial"


# ------------------------------------------------------------------
# Device info from config
# ------------------------------------------------------------------

class TestDeviceInfo:
    def test_get_name(self):
        conn = _make_conn({'device_name': 'MyRepeater'})
        assert conn.get_name() == 'MyRepeater'

    def test_get_name_empty_returns_none(self):
        conn = _make_conn({'device_name': ''})
        assert conn.get_name() is None

    def test_get_name_missing_returns_none(self):
        conn = _make_conn()
        assert conn.get_name() is None

    def test_get_pubkey_uppercases(self):
        conn = _make_conn({'public_key': 'ab' * 32})
        assert conn.get_pubkey() == 'AB' * 32

    def test_get_pubkey_empty_returns_none(self):
        conn = _make_conn({'public_key': ''})
        assert conn.get_pubkey() is None

    def test_get_radio_info(self):
        conn = _make_conn({'radio_info': 'SF9 BW125'})
        assert conn.get_radio_info() == 'SF9 BW125'

    def test_get_radio_info_missing_returns_none(self):
        conn = _make_conn()
        assert conn.get_radio_info() is None

    def test_get_privkey_always_none(self):
        conn = _make_conn({'private_key': 'should_be_ignored'})
        assert conn.get_privkey() is None

    def test_get_firmware_version_always_none(self):
        assert _make_conn().get_firmware_version() is None

    def test_get_board_type_always_none(self):
        assert _make_conn().get_board_type() is None

    def test_get_device_stats_always_empty(self):
        assert _make_conn().get_device_stats() == {}

    def test_set_time_is_noop(self):
        _make_conn().set_time()  # must not raise

    def test_execute_command_returns_false(self):
        conn = _make_conn()
        success, msg = conn.execute_command("get name")
        assert success is False
        assert "BLE" in msg
