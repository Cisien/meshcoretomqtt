import socket
import struct
import logging
from pathlib import Path
import tempfile
import threading
import time

import pytest

from bridge.coc_connection import (
    CocConnection, HELLO, READY, RECORD, RECEIPT, COMMAND, REPLY,
    encode_packet, decode_packet, send_frame, recv_frame, validate_coc_config,
    _helper_failure_category,
)
from bridge.runner import connect_transport
from config_loader import resolve_transport


def frame(kind, session, ident, payload=b""):
    return encode_packet(kind, session, ident, payload)


@pytest.fixture(autouse=True)
def isolate_coc_test_sessions():
    # Test-only process state: reconnect tests retain it within a test, not across tests.
    from bridge import coc_connection
    for phase in range(2):
        with coc_connection._STATE_LOCK:
            coc_connection._PEER_STATES.clear()
            coc_connection._ACTIVE_GENERATIONS.clear()
            coc_connection._GENERATION_COUNTERS.clear()
        if phase == 0:
            yield


def start_pair(session=77, queue_limit=2, peer="synthetic-peer"):
    host_data, device_data = socket.socketpair()
    host_control, device_control = socket.socketpair()
    device_data.settimeout(.15); device_control.settimeout(.15)
    send_frame(device_data, frame(HELLO, session, 0, b"\x20\x02"))
    send_frame(device_control, frame(HELLO, session, 0, b"\x20\x02"))
    connection = CocConnection.from_sockets(
        host_data, host_control, session,
        peer_properties={"Paired": True, "Trusted": True, "Bonded": True, "encrypted": True},
        peer=peer, queue_limit=queue_limit,
    )
    connection.start()
    assert decode_packet(recv_frame(device_data), "device").kind == READY
    assert decode_packet(recv_frame(device_control), "device").kind == READY
    return connection, device_data, device_control


def test_coc_is_explicit_and_serial_remains_default():
    config = {"general": {"transport": "coc"}}
    assert resolve_transport(config) == "coc"
    assert resolve_transport({}) == "serial"
    with pytest.raises(ValueError):
        resolve_transport({"general": {"transport": "ble"}})
    with pytest.raises(ValueError):
        resolve_transport({"transport": "coc"})
    marker = object()
    assert connect_transport(config, coc_factory=lambda actual: marker) is marker


def test_failed_startup_getter_closes_the_device():
    from bridge.runner import run
    from tests.fakes import FakeSerialConnection, make_test_state
    device = FakeSerialConnection(name=None)
    state = make_test_state(config={"general": {"transport": "coc", "sync_time": False}})
    run(state, transport_factory=lambda config: device)
    assert not device.is_open


def test_records_are_admitted_before_receipt_and_duplicates_survive_reconnect():
    conn, data, control = start_pair(session=901, queue_limit=1)
    send_frame(data, frame(RECORD, 901, 1, b"first\r\n"))
    receipt = decode_packet(recv_frame(control), "device")
    assert (receipt.kind, receipt.session, receipt.ident) == (RECEIPT, 901, 1)

    send_frame(data, frame(RECORD, 901, 2, b"second\r\n"))
    with pytest.raises(TimeoutError):
        recv_frame(control)
    assert conn.read_line() == "first"
    send_frame(data, frame(RECORD, 901, 2, b"second\r\n"))
    assert decode_packet(recv_frame(control), "device").ident == 2
    conn.close(); data.close(); control.close()

    reconnected, data2, control2 = start_pair(session=901, queue_limit=2)
    send_frame(data2, frame(RECORD, 901, 2, b"second\r\n"))
    assert decode_packet(recv_frame(control2), "device").ident == 2
    assert reconnected.read_line() == "second"
    assert reconnected.read_line() is None
    reconnected.close(); data2.close(); control2.close()


def test_command_reply_ids_stats_shape_and_control_independent_of_data_stall():
    conn, data, control = start_pair(session=902, queue_limit=1)
    send_frame(data, frame(RECORD, 902, 1, b"held\r\n"))
    assert decode_packet(recv_frame(control), "device").kind == RECEIPT
    result = []
    worker = threading.Thread(target=lambda: result.append(conn.execute_command("get name", 1)))
    worker.start()
    command = decode_packet(recv_frame(control), "device")
    assert command.kind == COMMAND and command.ident > 0
    send_frame(control, frame(REPLY, 902, command.ident, b"-> > repeater\r\n"))
    worker.join(1)
    assert result == [(True, "repeater")]
    assert conn.execute_command("reboot", .01)[0] is False
    conn.close(); data.close(); control.close()


def test_unsecured_peer_is_rejected_without_fallback():
    data, _ = socket.socketpair(); control, _ = socket.socketpair()
    with pytest.raises(PermissionError):
        CocConnection.from_sockets(data, control, 1, peer_properties={"Paired": True})
    data.close(); control.close()


def test_session_retirement_reconnect_accepts_new_session_and_rejects_old_one():
    conn, data, control = start_pair(session=1001, peer="retirement-peer")
    newer, data2, control2 = start_pair(session=1002, peer="retirement-peer")
    send_frame(data, frame(RECORD, 1001, 1, b"retired\r\n"))
    with pytest.raises(TimeoutError):
        recv_frame(control)
    conn.close(); data.close(); control.close()
    send_frame(data2, frame(RECORD, 1002, 1, b"new boot\r\n"))
    assert decode_packet(recv_frame(control2), "device").ident == 1
    assert newer.read_line() == "new boot"
    newer.close(); data2.close(); control2.close()

    stale_data, stale_device_data = socket.socketpair()
    stale_control, stale_device_control = socket.socketpair()
    send_frame(stale_device_data, frame(HELLO, 1001, 0, b"\x20\x02"))
    send_frame(stale_device_control, frame(HELLO, 1001, 0, b"\x20\x02"))
    stale = CocConnection.from_sockets(stale_data, stale_control, 1001,
                                       peer_properties={"Paired": True, "Trusted": True,
                                                        "Bonded": True, "encrypted": True},
                                       peer="retirement-peer")
    with pytest.raises(ValueError, match="stale"):
        stale.start()
    stale.close(); stale_device_data.close(); stale_device_control.close()


def test_device_stats_are_reassembled_to_serial_connection_shape():
    conn, data, control = start_pair(session=1003, peer="stats-peer")
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(conn.get_device_stats()))
    worker.start()
    responses = {
        "stats-core": b'{"battery_mv":4100,"uptime_secs":10,"errors":3,"queue_len":2}',
        "stats-radio": b'{"noise_floor":-99,"tx_air_secs":4,"rx_air_secs":5}',
        "stats-packets": b'{"recv_errors":6}',
    }
    seen = []
    for _ in range(3):
        cmd = decode_packet(recv_frame(control), "device")
        command = cmd.payload.decode()
        seen.append(command)
        send_frame(control, frame(REPLY, 1003, cmd.ident, responses[command]))
    worker.join(1)
    assert set(seen) == set(responses)
    assert outcome == [{"battery_mv": 4100, "uptime_secs": 10, "debug_flags": 3,
                        "queue_len": 2, "noise_floor": -99, "tx_air_secs": 4,
                        "rx_air_secs": 5, "recv_errors": 6}]
    conn.close(); data.close(); control.close()


def test_coc_config_requires_pinned_peer_and_two_distinct_absolute_ipc_paths():
    config = {"coc": {"address": "02:00:00:00:00:01", "helper": "/opt/mctomqtt/coc-helper",
                      "adapter": "hci0", "data_socket": "/tmp/data.sock",
                      "control_socket": "/tmp/control.sock"}}
    assert validate_coc_config(config)["address"] == "02:00:00:00:00:01"
    invalid = dict(config["coc"], control_socket="/tmp/data.sock")
    with pytest.raises(ValueError):
        validate_coc_config({"coc": invalid})


def test_missing_helper_returns_no_device_without_serial_or_ble_fallback(tmp_path):
    config = {"general": {"transport": "coc"}, "coc": {
        "address": "02:00:00:00:00:01", "adapter": "hci0", "helper": str(tmp_path / "missing-helper"),
        "data_socket": str(tmp_path / "data.sock"), "control_socket": str(tmp_path / "control.sock"),
    }}
    forbidden = lambda _: pytest.fail("CoC failure must not trigger fallback")
    assert connect_transport(config, serial_factory=forbidden) is None


def test_helper_failure_logs_exception_category_without_path_or_backend_text(tmp_path, caplog):
    helper = tmp_path / "DO_NOT_LOG_SECRET_HELPER_PATH"
    helper.write_text("not executable")
    config = {"general": {"transport": "coc"}, "coc": {
        "address": "02:00:00:00:00:01", "adapter": "hci0", "helper": str(helper),
        "data_socket": str(tmp_path / "data.sock"), "control_socket": str(tmp_path / "control.sock"),
    }}
    with caplog.at_level(logging.WARNING, logger="bridge.coc_connection"):
        assert connect_transport(config, serial_factory=lambda _: pytest.fail("must not fall back")) is None
    assert "permission" in caplog.text
    assert str(helper) not in caplog.text


def test_helper_failure_category_is_fixed_and_never_returns_exception_text():
    assert _helper_failure_category(ValueError("unsupported CoC protocol version 2")) == "unsupported_version_2"
    assert _helper_failure_category(ValueError("zero CoC session")) == "zero_session"
    assert _helper_failure_category(ValueError("invalid CoC packet direction")) == "wrong_direction"
    assert _helper_failure_category(ValueError("SECRET PAYLOAD")) == "invalid_value"


def test_production_adapter_with_test_only_rust_helper_runs_full_socket_path():
    helper = Path(__file__).parents[1] / "coc-helper/target/debug/observer-coc-helper"
    if not helper.is_file():
        pytest.skip("build the offline Rust helper before running its process integration test")
    # Unix socket paths must fit even in long checkout/output directory names.
    with tempfile.TemporaryDirectory(prefix="coc-ipc-") as directory:
        base = Path(directory) / "ipc"
        config = {"coc": {
            "address": "02:00:00:00:00:01", "adapter": "hci0", "helper": str(helper),
            "data_socket": str(base) + "-data.sock", "control_socket": str(base) + "-control.sock",
            "startup_timeout": 4,
        }}
        conn = CocConnection.launch(config, test_mode=True)
        process = conn._proc
        try:
            deadline = time.monotonic() + 1
            line = None
            while line is None and time.monotonic() < deadline:
                line = conn.read_line()
                time.sleep(.005)
            assert line == "synthetic"
            assert conn.execute_command("get name", 1) == (True, "synthetic-name")
        finally:
            conn.close()
        assert process.poll() is not None
        assert not Path(config["coc"]["data_socket"]).exists()
        assert not Path(config["coc"]["control_socket"]).exists()


def test_helper_fixed_stage_stderr_is_inherited_for_service_diagnostics(tmp_path, capfd):
    helper = tmp_path / "safe-stage-helper"
    helper.write_text('''#!/usr/bin/env python3
import socket, struct, sys
args = sys.argv[1:]
data = args[args.index("--data-socket") + 1]
control = args[args.index("--control-socket") + 1]
listeners = []
for path in (data, control):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path); listener.listen(1); listeners.append(listener)
sys.stderr.write("CoC stage=test_helper_ready\\n"); sys.stderr.flush()
session = 0x102030405060708
packet = struct.pack("<BBQI", 1, HELLO := 1, session, 0) + b"\\x20\\x02"
connections = [listener.accept()[0] for listener in listeners]
for conn in connections:
    conn.sendall(struct.pack("<H", len(packet)) + packet)
def read_exact(conn, count):
    out = bytearray()
    while len(out) < count:
        part = conn.recv(count - len(out))
        if not part: raise SystemExit(2)
        out.extend(part)
    return bytes(out)
for conn in connections:
    size = struct.unpack("<H", read_exact(conn, 2))[0]
    read_exact(conn, size)
connections[0].settimeout(10)
# Stay connected until the receiver closes; immediate exit races is_open.
while connections[0].recv(1): pass
for conn in connections: conn.close()
for listener in listeners: listener.close()
''')
    helper.chmod(0o755)
    base = tmp_path / "ipc"
    config = {"coc": {
        "address": "02:00:00:00:00:01", "adapter": "hci0", "helper": str(helper),
        "data_socket": str(base) + "-data.sock", "control_socket": str(base) + "-control.sock",
        "startup_timeout": 4,
    }}
    conn = CocConnection.launch(config, test_mode=True)
    try:
        assert conn.is_open
        assert "CoC stage=test_helper_ready" in capfd.readouterr().err
    finally:
        conn.close()
