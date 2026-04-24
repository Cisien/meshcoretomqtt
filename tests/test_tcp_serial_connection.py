"""Tests for TcpSerialConnection behavior with mock socket-backed serial ports."""
from __future__ import annotations

from unittest.mock import MagicMock

import serial

from bridge.serial_connection import RealSerialConnection, TcpSerialConnection, connect


def _make_tcp_conn(read_until_value: bytes | list[bytes] = b"") -> tuple[TcpSerialConnection, MagicMock]:
    """Create a TcpSerialConnection with a mock socket-backed serial port."""
    mock_port = MagicMock(spec=serial.Serial)
    mock_port.is_open = True
    mock_port.timeout = 2
    mock_port.portstr = "socket://192.168.1.123:4403"
    if isinstance(read_until_value, list):
        mock_port.read_until.side_effect = read_until_value
    else:
        mock_port.read_until.return_value = read_until_value
    conn = TcpSerialConnection(mock_port)
    return conn, mock_port


class TestTcpGetName:
    def test_parses_bare_value_response(self):
        conn, _ = _make_tcp_conn(b"TestNode\n")
        assert conn.get_name() == "TestNode"

    def test_parses_arrow_response(self):
        conn, _ = _make_tcp_conn(b"get name\n  -> >MyRepeater\n> ")
        assert conn.get_name() == "MyRepeater"

    def test_parses_arrow_response_without_prompt_marker(self):
        conn, _ = _make_tcp_conn(b"get name\n  -> MyRepeater\n> ")
        assert conn.get_name() == "MyRepeater"

    def test_waits_for_delayed_response(self):
        conn, _ = _make_tcp_conn([
            b"",
            b"get name\n  ->",
            b" >DelayedNode\n",
            b"> ",
        ])
        assert conn.get_name() == "DelayedNode"

    def test_ignores_prompt_only_before_response(self):
        conn, _ = _make_tcp_conn([
            b"> ",
            b"get name\n  -> MeshNode\n> ",
        ])
        assert conn.get_name() == "MeshNode"

    def test_ignores_unsolicited_line_before_reply(self):
        conn, _ = _make_tcp_conn([
            b"12:34:56 - 22/4/2026 U: RX, len=12 (type=5, route=F, payload_len=8)\n",
            b"get name\n  -> MeshNode\n> ",
        ])
        assert conn.get_name() == "MeshNode"

    def test_ignores_unsolicited_line_before_bare_reply(self):
        conn, _ = _make_tcp_conn([
            b"12:34:56 - 22/4/2026 U: RX, len=12 (type=5, route=F, payload_len=8)\n",
            b"TestNode\n",
        ])
        assert conn.get_name() == "TestNode"


class TestTcpGetPubkey:
    def test_valid_64_hex_bare_value(self):
        key = "aa" * 32
        conn, _ = _make_tcp_conn(f"{key}\n".encode())
        assert conn.get_pubkey() == key.upper()

    def test_valid_64_hex_without_prompt_marker(self):
        key = "aa" * 32
        conn, _ = _make_tcp_conn(f"get public.key\n  -> {key}\n> ".encode())
        assert conn.get_pubkey() == key.upper()


class TestTcpGetFirmwareVersion:
    def test_ignores_unsolicited_line_before_reply(self):
        conn, _ = _make_tcp_conn([
            b"12:34:56 - 22/4/2026 U RAW: 0A1B2C3D\n",
            b"ver\n  -> 1.8.2-dev-834c700 (Build: 04-Sep-2025)\n> ",
        ])
        assert conn.get_firmware_version() == "1.8.2-dev-834c700 (Build: 04-Sep-2025)"


class TestTcpExecuteCommand:
    def test_success(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.timeout = 2
        mock_port.portstr = "socket://192.168.1.123:4403"
        mock_port.read_until.return_value = b"ver\n  -> 1.8.2\n> "
        conn = TcpSerialConnection(mock_port)
        success, response = conn.execute_command("ver")
        assert success is True
        assert "1.8.2" in response


class TestTcpReadLine:
    def test_socket_accumulates_chunked_line_across_reads(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.timeout = 2
        mock_port.portstr = "socket://192.168.1.123:4403"
        mock_port.in_waiting = 1
        mock_port.read.side_effect = [
            b"12:34:56 - 1/15/2025 U: RX, len=32 ",
            b"",
            b"(type=2, route=F, payload_len=16)\n",
        ]
        conn = TcpSerialConnection(mock_port)

        assert conn.read_line() is None
        assert conn.read_line() == "12:34:56 - 1/15/2025 U: RX, len=32 (type=2, route=F, payload_len=16)"

    def test_strips_leading_prompt_from_live_line(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.timeout = 2
        mock_port.portstr = "socket://192.168.1.123:4403"
        mock_port.in_waiting = 10
        mock_port.read.return_value = b"> 12:34:56 - 1/15/2025 U: RX, len=32 (type=2, route=F, payload_len=16)\n"
        conn = TcpSerialConnection(mock_port)
        assert conn.read_line() == "12:34:56 - 1/15/2025 U: RX, len=32 (type=2, route=F, payload_len=16)"

    def test_ignores_prompt_only_line(self):
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True
        mock_port.timeout = 2
        mock_port.portstr = "socket://192.168.1.123:4403"
        mock_port.in_waiting = 2
        mock_port.read.side_effect = [b"> \n", b""]
        conn = TcpSerialConnection(mock_port)
        assert conn.read_line() is None


class TestTcpConnectFactory:
    def test_uses_tcp_connection_when_enabled(self, monkeypatch):
        config = {
            'serial': {
                'baud_rate': 115200,
                'timeout': 2,
            },
            'tcp_serial': {
                'enabled': True,
                'address': ['socket://192.168.1.123:4403'],
            },
        }
        mock_port = MagicMock()
        mock_port.is_open = True
        urls: list[str] = []

        def fake_serial_for_url(url, *args, **kwargs):
            urls.append(url)
            return mock_port

        monkeypatch.setattr(serial, 'serial_for_url', fake_serial_for_url)

        result = connect(config)

        assert isinstance(result, TcpSerialConnection)
        assert urls == ['socket://192.168.1.123:4403']
        mock_port.write.assert_not_called()

    def test_returns_none_when_all_tcp_endpoints_fail(self, monkeypatch):
        config = {
            'serial': {
                'baud_rate': 115200,
                'timeout': 2,
            },
            'tcp_serial': {
                'enabled': True,
                'address': ['socket://192.168.1.123:4403'],
            },
        }

        def fake_serial_for_url(*args, **kwargs):
            raise serial.SerialException("nope")

        monkeypatch.setattr(serial, 'serial_for_url', fake_serial_for_url)

        result = connect(config)
        assert result is None

    def test_returns_none_when_tcp_enabled_without_address(self):
        config = {
            'serial': {
                'baud_rate': 115200,
                'timeout': 2,
            },
            'tcp_serial': {
                'enabled': True,
                'address': [],
            },
        }

        result = connect(config)
        assert result is None

    def test_does_not_fallback_to_serial_when_tcp_enabled(self, monkeypatch):
        config = {
            'serial': {
                'ports': ['/dev/ttyACM0'],
                'baud_rate': 115200,
                'timeout': 2,
            },
            'tcp_serial': {
                'enabled': True,
                'address': ['socket://192.168.1.123:4403'],
            },
        }

        def fake_serial(*args, **kwargs):
            raise AssertionError("Serial fallback should not be attempted")

        def fake_serial_for_url(*args, **kwargs):
            raise serial.SerialException("nope")

        monkeypatch.setattr(serial, 'Serial', fake_serial)
        monkeypatch.setattr(serial, 'serial_for_url', fake_serial_for_url)

        result = connect(config)
        assert result is None


class TestConnectFactoryDefaults:
    def test_serial_remains_default_when_tcp_disabled(self, monkeypatch):
        config = {
            'serial': {
                'ports': ['/dev/ttyACM0'],
                'baud_rate': 115200,
                'timeout': 2,
            },
            'tcp_serial': {
                'enabled': False,
                'address': ['socket://192.168.1.123:4403'],
            },
        }
        mock_port = MagicMock(spec=serial.Serial)
        mock_port.is_open = True

        def fake_serial(*args, **kwargs):
            return mock_port

        monkeypatch.setattr(serial, 'Serial', fake_serial)

        result = connect(config)
        assert isinstance(result, RealSerialConnection)

    def test_accepts_legacy_string_address(self, monkeypatch):
        config = {
            'serial': {
                'baud_rate': 115200,
                'timeout': 2,
            },
            'tcp_serial': {
                'enabled': True,
                'address': 'socket://192.168.1.123:4403',
            },
        }
        mock_port = MagicMock()
        mock_port.is_open = True
        urls: list[str] = []

        def fake_serial_for_url(url, *args, **kwargs):
            urls.append(url)
            return mock_port

        monkeypatch.setattr(serial, 'serial_for_url', fake_serial_for_url)

        result = connect(config)

        assert isinstance(result, TcpSerialConnection)
        assert urls == ['socket://192.168.1.123:4403']
