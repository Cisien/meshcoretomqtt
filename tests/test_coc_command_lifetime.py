"""Real Unix IPC reproductions of command ordering and queued expiry."""
import socket
import threading
import uuid

import pytest

from bridge.coc_connection import (
    CocConnection, HELLO, READY, COMMAND, REPLY,
    encode_packet, decode_packet, send_frame, recv_frame,
)


def connected(cls):
    data, radio_data = socket.socketpair()
    control, radio_control = socket.socketpair()
    radio_control.settimeout(.3)
    connection = cls.from_sockets(
        data, control, 1234, peer="request-test-" + uuid.uuid4().hex,
        peer_properties={key: True for key in ("Paired", "Trusted", "Bonded", "encrypted")},
    )
    for channel in (radio_data, radio_control):
        send_frame(channel, encode_packet(HELLO, 1234, 0, b"\x20\x02"))
    connection.start()
    for channel in (radio_data, radio_control):
        assert decode_packet(recv_frame(channel), "device").kind == READY
    return connection, radio_data, radio_control


def test_concurrent_callers_transmit_monotonically_increasing_request_ids():
    class PausedFirstCaller(CocConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.first = threading.Event()
            self.second = threading.Event()
            self.release = threading.Event()

        def _queue_control(self, kind, ident, payload=b"", **kwargs):
            if kind == COMMAND and ident == 1:
                self.first.set()
                assert self.release.wait(2)
            elif kind == COMMAND and ident == 2:
                self.second.set()
            return super()._queue_control(kind, ident, payload, **kwargs)

    conn, data, control = connected(PausedFirstCaller)
    replies = []
    workers = [threading.Thread(target=lambda: replies.append(conn.execute_command("get name", 2))) for _ in range(2)]
    try:
        workers[0].start()
        assert conn.first.wait(1)
        workers[1].start()
        conn.second.wait(.1)
        conn.release.set()
        ids = []
        for _ in range(2):
            packet = decode_packet(recv_frame(control), "device")
            ids.append(packet.ident)
            send_frame(control, encode_packet(REPLY, 1234, packet.ident, b"name"))
        for worker in workers:
            worker.join(2)
        assert ids == [1, 2]
        assert replies == [(True, "name"), (True, "name")]
    finally:
        conn.release.set()
        conn.close()
        data.close()
        control.close()
        for worker in workers:
            if worker.ident is not None:
                worker.join(2)


def test_expired_queued_clock_command_is_not_transmitted_later():
    class PausedWriter(CocConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.release = threading.Event()

        def _write_control(self):
            assert self.release.wait(2)
            super()._write_control()

    conn, data, control = connected(PausedWriter)
    try:
        assert conn.execute_command("time 1793232000", 0) == (False, "Command timeout")
        conn.release.set()
        with pytest.raises(TimeoutError):
            recv_frame(control)
        # Expiry must release command capacity and leave subsequent requests usable.
        replies = []
        worker = threading.Thread(target=lambda: replies.append(conn.execute_command("get name", 1)))
        worker.start()
        packet = decode_packet(recv_frame(control), "device")
        assert packet.kind == COMMAND and packet.ident == 2
        send_frame(control, encode_packet(REPLY, 1234, packet.ident, b"name"))
        worker.join(2)
        assert replies == [(True, "name")]
    finally:
        conn.release.set()
        conn.close()
        data.close()
        control.close()
