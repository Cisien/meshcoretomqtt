"""Tests for RealSerialConnection parsing logic with mock serial.Serial."""
from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import serial
import pytest

from bridge.serial_connection import RealSerialConnection, connect


def _make_conn(read_all_value: bytes | list[bytes] = b"") -> tuple[RealSerialConnection, MagicMock]:
    """Create a RealSerialConnection with a mock serial port."""
    mock_port = MagicMock(spec=serial.Serial)
    mock_port.is_open = True
    if isinstance(read_all_value, list):
        mock_port.read_all.side_effect = read_all_value
    else:
        mock_port.read_all.return_value = read_all_value
    conn = RealSerialConnection(mock_port)
    return conn, mock_port


# ------------------------------------------------------------------
# get_name
# ------------------------------------------------------------------

class TestGetName:
    def test_parses_arrow_response(self):
        conn, _ = _make_conn(b"get name\n  -> >MyRepeater\n> ")
        assert conn.get_name() == "MyRepeater"

    def test_returns_none_on_garbage(self):
        conn, _ = _make_conn(b"something unexpected\n> ")
        assert conn.get_name() is None

    def test_strips_whitespace_and_cr(self):
        conn, _ = _make_conn(b"get name\n  -> >  TestNode \r\n> ")
        assert conn.get_name() == "TestNode"

    def test_multiline_takes_first(self):
        conn, _ = _make_conn(b"get name\n  -> >NodeA\nNodeB\n> ")
        assert conn.get_name() == "NodeA"


# ------------------------------------------------------------------
# get_pubkey
# ------------------------------------------------------------------

class TestGetPubkey:
    def test_valid_64_hex(self):
        key = "aa" * 32
        conn, _ = _make_conn(f"get public.key\n  -> >{key}\n> ".encode())
        assert conn.get_pubkey() == key.upper()

    def test_rejects_short_key(self):
        conn, _ = _make_conn(b"get public.key\n  -> >ABCD\n> ")
        assert conn.get_pubkey() is None

    def test_rejects_non_hex(self):
        key = "GG" * 32  # not hex
        conn, _ = _make_conn(f"get public.key\n  -> >{key}\n> ".encode())
        assert conn.get_pubkey() is None

    def test_normalizes_to_uppercase(self):
        key = "ab" * 32
        conn, _ = _make_conn(f"get public.key\n  -> >{key}\n> ".encode())
        assert conn.get_pubkey() == key.upper()


# ------------------------------------------------------------------
# get_privkey
# ------------------------------------------------------------------

class TestGetPrivkey:
    def test_valid_128_hex(self):
        key = "cc" * 64
        conn, _ = _make_conn(f"get prv.key\n  -> >{key}\n> ".encode())
        assert conn.get_privkey() == key

    def test_rejects_wrong_length(self):
        conn, _ = _make_conn(b"get prv.key\n  -> >AABB\n> ")
        assert conn.get_privkey() is None

    def test_rejects_non_hex(self):
        key = "ZZ" * 64
        conn, _ = _make_conn(f"get prv.key\n  -> >{key}\n> ".encode())
        assert conn.get_privkey() is None


# ------------------------------------------------------------------
# get_radio_info
# ------------------------------------------------------------------

class TestGetRadioInfo:
    def test_parses_response(self):
        conn, _ = _make_conn(b"get radio\n  -> >LoRa 915MHz SF10 BW250\n> ")
        assert conn.get_radio_info() == "LoRa 915MHz SF10 BW250"

    def test_returns_none_on_garbage(self):
        conn, _ = _make_conn(b"unexpected\n> ")
        assert conn.get_radio_info() is None


# ------------------------------------------------------------------
# get_firmware_version
# ------------------------------------------------------------------

class TestGetFirmwareVersion:
    def test_parses_format(self):
        conn, _ = _make_conn(b"ver\n  -> 1.8.2-dev-834c700 (Build: 04-Sep-2025)\n> ")
        assert conn.get_firmware_version() == "1.8.2-dev-834c700 (Build: 04-Sep-2025)"

    def test_returns_none_on_garbage(self):
        conn, _ = _make_conn(b"unexpected\n> ")
        assert conn.get_firmware_version() is None


# ------------------------------------------------------------------
# get_board_type
# ------------------------------------------------------------------

class TestGetBoardType:
    def test_parses_response(self):
        conn, _ = _make_conn(b"board\n  -> Station G2\n> ")
        assert conn.get_board_type() == "Station G2"

    def test_unknown_command(self):
        conn, _ = _make_conn(b"board\n  -> Unknown command\n> ")
        assert conn.get_board_type() == "unknown"

    def test_returns_none_on_garbage(self):
        conn, _ = _make_conn(b"unexpected\n> ")
        assert conn.get_board_type() is None


# ------------------------------------------------------------------
# get_device_stats
# ------------------------------------------------------------------

class TestGetDeviceStats:
    def test_core_stats(self):
        conn, mock_port = _make_conn([
            b'stats-core\n  -> {"battery_mv":4200,"uptime_secs":3600,"errors":0,"queue_len":5}\n> ',
            b'stats-radio\n  -> Unknown command\n> ',
            b'stats-packets\n  -> Unknown command\n> ',
        ])
        stats = conn.get_device_stats()
        assert stats['battery_mv'] == 4200
        assert stats['uptime_secs'] == 3600
        assert stats['debug_flags'] == 0
        assert stats['queue_len'] == 5

    def test_radio_stats(self):
        conn, _ = _make_conn([
            b'stats-core\n  -> Unknown command\n> ',
            b'stats-radio\n  -> {"noise_floor":-100,"tx_air_secs":10,"rx_air_secs":20}\n> ',
            b'stats-packets\n  -> Unknown command\n> ',
        ])
        stats = conn.get_device_stats()
        assert stats['noise_floor'] == -100
        assert stats['tx_air_secs'] == 10
        assert stats['rx_air_secs'] == 20

    def test_packets_stats(self):
        conn, _ = _make_conn([
            b'stats-core\n  -> Unknown command\n> ',
            b'stats-radio\n  -> Unknown command\n> ',
            b'stats-packets\n  -> {"recv_errors":42}\n> ',
        ])
        stats = conn.get_device_stats()
        assert stats['recv_errors'] == 42

    def test_partial_support(self):
        """Firmware that only supports some stat commands."""
        conn, _ = _make_conn([
            b'stats-core\n  -> {"battery_mv":3800}\n> ',
            b'stats-radio\n  -> Unknown command\n> ',
            b'stats-packets\n  -> Unknown command\n> ',
        ])
        stats = conn.get_device_stats()
        assert stats == {'battery_mv': 3800}

    def test_invalid_json(self):
        conn, _ = _make_conn([
            b'stats-core\n  -> {invalid json}\n> ',
            b'stats-radio\n  -> {"noise_floor":-90}\n> ',
            b'stats-packets\n  -> Unknown command\n> ',
        ])
        stats = conn.get_device_stats()
        assert 'battery_mv' not in stats
        assert stats['noise_floor'] == -90


# ------------------------------------------------------------------
# execute_command
# ------------------------------------------------------------------

class TestExecuteCommand:
    def test_success(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.in_waiting = 5
        mock_port.read_all.return_value = b"ver\n  -> 1.8.2\n> "
        conn = RealSerialConnection(mock_port)
        success, response = conn.execute_command("ver")
        assert success is True
        assert "1.8.2" in response

    def test_strips_echo(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.in_waiting = 5
        mock_port.read_all.return_value = b"get name\n  -> >TestNode\n> "
        conn = RealSerialConnection(mock_port)
        success, response = conn.execute_command("get name")
        assert success is True


# ------------------------------------------------------------------
# read_line
# ------------------------------------------------------------------

class TestReadLine:
    def test_returns_none_when_empty(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.in_waiting = 0
        conn = RealSerialConnection(mock_port)
        assert conn.read_line() is None

    def test_returns_decoded_line(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.in_waiting = 10
        mock_port.readline.return_value = b"test line\n"
        conn = RealSerialConnection(mock_port)
        assert conn.read_line() == "test line"


# ------------------------------------------------------------------
# close
# ------------------------------------------------------------------

class TestClose:
    def test_close_handles_already_closed(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = False
        conn = RealSerialConnection(mock_port)
        conn.close()  # Should not raise


# ------------------------------------------------------------------
# connect factory
# ------------------------------------------------------------------

class TestConnectFactory:
    def test_returns_none_when_all_fail(self):
        config = {
            'serial': {
                'ports': ['/dev/nonexistent1', '/dev/nonexistent2'],
                'baud_rate': 115200,
                'timeout': 2,
            }
        }
        result = connect(config)
        assert result is None
