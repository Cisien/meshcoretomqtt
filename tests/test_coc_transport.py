import socket
import struct
import threading
import time
import pytest

from bridge.coc_connection import CocConnection, encode_packet, decode_packet, send_frame


def packet(kind, session, ident, payload=b""):
    return encode_packet(kind, session, ident, payload)


def test_protocol_roundtrip_and_rejects_wrong_direction():
    wire = packet(3, 7, 9, b"abc")
    assert decode_packet(wire, "host").payload == b"abc"
    try:
        decode_packet(wire, "device")
    except ValueError:
        pass
    else:
        assert False, "record must not be accepted from device direction"


@pytest.mark.parametrize("prefix", ["> ", "-> > ", "-> ", ""])
def test_cli_reply_normalization_accepts_serial_cli_prefixes(prefix):
    key = "ab" * 64
    assert CocConnection._extract_response("get prv.key", prefix + key) == key
    assert CocConnection._extract_response("get name", prefix + "node") == "node"


def test_socketpair_stream_delivers_once_and_emits_exact_receipt():
    host_data, device_data = socket.socketpair()
    host_control, device_control = socket.socketpair()
    conn = CocConnection.from_sockets(host_data, host_control, 0x1234,
                                      peer_properties={"Paired": True, "Trusted": True,
                                                       "Bonded": True, "encrypted": True})
    send_frame(device_data, packet(1, 0x1234, 0, b"\x20\x02"))
    send_frame(device_control, packet(1, 0x1234, 0, b"\x20\x02"))
    conn.start()
    read_frame(device_data); read_frame(device_control)
    send_frame(device_data, packet(3, 0x1234, 41, b"line\r\n"))
    deadline = time.monotonic() + 1
    line = None
    while line is None and time.monotonic() < deadline:
        line = conn.read_line()
        time.sleep(.005)
    assert line == "line"
    receipt = decode_packet(read_frame(device_control), "device")
    assert receipt.kind == 4
    assert receipt.ident == 41
    conn.close()
    device_data.close(); device_control.close()


def test_security_gate_and_matched_command_reply_id():
    host_data, device_data = socket.socketpair()
    host_control, device_control = socket.socketpair()
    with pytest.raises(PermissionError):
        CocConnection.from_sockets(host_data, host_control, 99, peer_properties={"Paired": True})
    conn = CocConnection.from_sockets(host_data, host_control, 99,
                                      peer_properties={"Paired": True, "Trusted": True,
                                                       "Bonded": True, "encrypted": True})
    send_frame(device_data, packet(1, 99, 0, b"\x20\x02"))
    send_frame(device_control, packet(1, 99, 0, b"\x20\x02"))
    conn.start()
    read_frame(device_data); read_frame(device_control)
    result = []
    worker = threading.Thread(target=lambda: result.append(conn.execute_command("get name", 1)))
    worker.start()
    command = decode_packet(read_frame(device_control), "device")
    assert command.kind == 5 and command.ident != 0
    send_frame(device_control, packet(6, 99, command.ident, b"reply"))
    worker.join(1)
    assert result == [(True, "reply")]
    conn.close(); device_data.close(); device_control.close()


def read_frame(sock):
    n = struct.unpack("<H", sock.recv(2))[0]
    raw = b""
    while len(raw) < n:
        raw += sock.recv(n-len(raw))
    return raw
