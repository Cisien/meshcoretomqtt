"""Explicitly selected CoC transport, supervised by the pinned Rust helper."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .serial_connection import SerialConnection

logger = logging.getLogger(__name__)
HEADER = struct.Struct("<BBQI")
LOCAL_LENGTH = struct.Struct("<H")
VERSION = 1
MAX_SDU = 558
MAX_RECORD_BYTES = 544
MAX_PEERS = 64
MAX_SESSION_HISTORY = 64
MAX_PENDING_COMMANDS = 32
MAX_PENDING_RECEIPTS = 32
MAX_CONTROL_QUEUE = MAX_PENDING_COMMANDS + MAX_PENDING_RECEIPTS
HELLO, READY, RECORD, RECEIPT, COMMAND, REPLY, STATS_REQUEST, STATS_REPLY = range(1, 9)
TO_DEVICE = {READY, RECEIPT, COMMAND, STATS_REQUEST}
READ_COMMANDS = frozenset({
    "get name", "get public.key", "get prv.key", "get radio", "ver", "board",
    "stats-core", "stats-radio", "stats-packets",
})
_ADDRESS_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")
_ADAPTER_RE = re.compile(r"hci[0-9]{1,3}\Z")
_HELPER_RE = re.compile(r"[^\x00-\x20]+\Z")
_STATE_LOCK = threading.RLock()
_PEER_STATES: OrderedDict[str, dict[str, Any]] = OrderedDict()
_ACTIVE_GENERATIONS: dict[str, int] = {}
_GENERATION_COUNTERS: dict[str, int] = {}


@dataclass
class _PendingCommand:
    event: threading.Event
    response: list[str]
    deadline: float
    claimed: bool = False


def encode_packet(kind: int, session: int, ident: int, payload: bytes = b"") -> bytes:
    _validate_payload(kind, ident, payload)
    if not 0 < session < 1 << 64:
        raise ValueError("invalid CoC session")
    return HEADER.pack(VERSION, kind, session, ident) + payload


def _validate_payload(kind: int, ident: int, payload: bytes) -> None:
    if kind not in range(1, 9):
        raise ValueError("invalid CoC kind")
    if kind in (HELLO, READY):
        if ident != 0 or payload != b"\x20\x02":
            raise ValueError("invalid CoC handshake")
    elif ident == 0:
        raise ValueError("invalid CoC packet id")
    elif kind in (RECEIPT, STATS_REQUEST):
        if payload:
            raise ValueError("unexpected CoC payload")
    elif kind == COMMAND:
        if not 1 <= len(payload) <= 95 or any(c in payload for c in (b"\0", b"\r", b"\n")):
            raise ValueError("invalid CoC command")
    elif not 1 <= len(payload) <= (128 if kind == STATS_REPLY else MAX_RECORD_BYTES):
        raise ValueError("invalid CoC payload length")


def decode_packet(data: bytes, direction: str):
    if not HEADER.size <= len(data) <= MAX_SDU:
        raise ValueError("invalid CoC SDU size")
    version, kind, session, ident = HEADER.unpack_from(data)
    payload = data[HEADER.size:]
    if version != VERSION:
        raise ValueError(f"unsupported CoC protocol version {version}")
    if not session:
        raise ValueError("zero CoC session")
    if (kind in TO_DEVICE) != (direction == "device"):
        raise ValueError("invalid CoC packet direction")
    _validate_payload(kind, ident, payload)
    return type("Packet", (), {"kind": kind, "session": session, "ident": ident, "payload": payload})()


def send_frame(sock: socket.socket, data: bytes) -> None:
    if not 1 <= len(data) <= MAX_SDU:
        raise ValueError("invalid local frame size")
    sock.sendall(LOCAL_LENGTH.pack(len(data)) + data)


def recv_frame(sock: socket.socket) -> bytes:
    def exact(n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            part = sock.recv(n - len(out))
            if not part:
                raise EOFError("helper IPC closed")
            out.extend(part)
        return bytes(out)

    size = LOCAL_LENGTH.unpack(exact(LOCAL_LENGTH.size))[0]
    if not 1 <= size <= MAX_SDU:
        raise ValueError("invalid helper IPC length")
    return exact(size)


def validate_coc_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("coc")
    if not isinstance(raw, dict):
        raise ValueError("coc must be a TOML table")
    address = raw.get("address")
    adapter = raw.get("adapter", "hci0")
    helper = raw.get("helper")
    data_socket = raw.get("data_socket")
    control_socket = raw.get("control_socket")
    if not isinstance(address, str) or not _ADDRESS_RE.fullmatch(address):
        raise ValueError("coc.address must be a pinned Bluetooth address")
    if not isinstance(adapter, str) or not _ADAPTER_RE.fullmatch(adapter):
        raise ValueError("coc.adapter must name an hci adapter")
    if not isinstance(helper, str) or not _HELPER_RE.fullmatch(helper) or not os.path.isabs(helper):
        raise ValueError("coc.helper must be an absolute executable path")
    paths = (data_socket, control_socket)
    if any(not isinstance(path, str) or not os.path.isabs(path) or len(path.encode()) > 100 for path in paths):
        raise ValueError("coc socket paths must be absolute Unix socket paths")
    if data_socket == control_socket:
        raise ValueError("CoC data and control socket paths must differ")
    startup_timeout = raw.get("startup_timeout", 12.0)
    if isinstance(startup_timeout, bool) or not isinstance(startup_timeout, (int, float)) or not 1 <= startup_timeout <= 60:
        raise ValueError("coc.startup_timeout must be between 1 and 60 seconds")
    queue_limit = raw.get("queue_limit", 64)
    if isinstance(queue_limit, bool) or not isinstance(queue_limit, int) or not 1 <= queue_limit <= 1024:
        raise ValueError("coc.queue_limit must be between 1 and 1024")
    return {"address": address.upper(), "adapter": adapter, "helper": helper,
            "data_socket": data_socket, "control_socket": control_socket,
            "startup_timeout": float(startup_timeout), "queue_limit": queue_limit}


def _peer_state(peer: str, queue_limit: int) -> dict[str, Any]:
    state = _PEER_STATES.get(peer)
    if state is None:
        if len(_PEER_STATES) >= MAX_PEERS:
            raise RuntimeError("CoC peer session capacity exhausted")
        state = {"current": None, "sessions": OrderedDict(), "lines": deque(),
                 "queue_limit": queue_limit, "gap": False}
        _PEER_STATES[peer] = state
    else:
        _PEER_STATES.move_to_end(peer)
    return state


def _new_generation(peer: str) -> int:
    with _STATE_LOCK:
        value = _GENERATION_COUNTERS.get(peer, 0) + 1
        _GENERATION_COUNTERS[peer] = value
        _ACTIVE_GENERATIONS[peer] = value
        return value


class CocConnection(SerialConnection):
    """SerialConnection over helper-owned, independent CoC IPC channels."""

    def __init__(self, data: socket.socket, control: socket.socket, *, peer: str,
                 queue_limit: int = 64, helper_process: subprocess.Popen | None = None,
                 expected_session: int | None = None):
        if data is control or queue_limit < 1:
            raise ValueError("invalid CoC IPC configuration")
        self.address = peer.upper()
        self._data, self._control = data, control
        self._proc = helper_process
        self._expected_session = expected_session
        self._queue_limit = queue_limit
        self._session: int | None = None
        self._generation = 0
        self._state: dict[str, Any] | None = None
        self._closed = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingCommand] = {}
        self._outbound: queue.PriorityQueue = queue.PriorityQueue(MAX_CONTROL_QUEUE)
        self._outbound_sequence = 0
        self._outbound_lock = threading.Lock()
        self._command_slots = threading.BoundedSemaphore(MAX_PENDING_COMMANDS)
        self._receipt_slots = threading.BoundedSemaphore(MAX_PENDING_RECEIPTS)
        self._next_id = 0
        self._last_activity = time.time()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._close_lock = threading.Lock()
        self._metrics = {"duplicates": 0, "admission_drops": 0, "malformed": 0}

    @classmethod
    def from_sockets(cls, data, control, session=None, *, peer_properties=None,
                     peer="TEST-PEER", queue_limit=64):
        """Offline-only socket injection seam; production uses launch()."""
        properties = peer_properties or {}
        if any(properties.get(k) is not True for k in ("Paired", "Trusted", "Bonded", "encrypted")):
            raise PermissionError("synthetic CoC peer must satisfy the security gate")
        return cls(data, control, peer=peer, queue_limit=queue_limit, expected_session=session)

    @classmethod
    def launch(cls, config: dict[str, Any], *, test_mode: bool = False):
        cfg = validate_coc_config(config)
        command = [cfg["helper"], "--peer", cfg["address"], "--adapter", cfg["adapter"],
                   "--data-socket", cfg["data_socket"], "--control-socket", cfg["control_socket"]]
        if test_mode:
            command = [cfg["helper"], "--test-mode", "--data-socket", cfg["data_socket"],
                       "--control-socket", cfg["control_socket"]]
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                # The helper emits fixed-stage diagnostics only; inherit stderr
                                # so service logs retain sanitized startup evidence.
                                stderr=None, close_fds=True)
        data = control = None
        deadline = time.monotonic() + cfg["startup_timeout"]
        try:
            data = _connect_unix_until(cfg["data_socket"], proc, deadline)
            control = _connect_unix_until(cfg["control_socket"], proc, deadline)
            conn = cls(data, control, peer=cfg["address"], queue_limit=cfg["queue_limit"], helper_process=proc)
            conn.start(timeout=max(.1, deadline - time.monotonic()))
            return conn
        except Exception:
            for sock in (data, control):
                if sock:
                    try: sock.close()
                    except OSError: pass
            _stop_process(proc)
            raise

    def start(self, timeout: float = 5.0) -> None:
        if self._started:
            return
        deadline = time.monotonic() + timeout
        for sock in (self._data, self._control):
            sock.settimeout(max(.01, deadline - time.monotonic()))
        data_hello = decode_packet(recv_frame(self._data), "host")
        control_hello = decode_packet(recv_frame(self._control), "host")
        if data_hello.kind != HELLO or control_hello.kind != HELLO or data_hello.session != control_hello.session:
            raise ValueError("invalid CoC channel HELLOs")
        if self._expected_session is not None and data_hello.session != self._expected_session:
            raise ValueError("CoC HELLO session did not match expected session")
        session = data_hello.session
        with _STATE_LOCK:
            self._state = _peer_state(self.address, self._queue_limit)
            known = self._state["sessions"]
            current = self._state["current"]
            if session in known and current != session:
                raise ValueError("stale CoC session rejected")
            if session not in known:
                if len(known) >= MAX_SESSION_HISTORY:
                    evict = next((old for old in known if old != current and not any(item[0] == old for item in self._state["lines"])), None)
                    if evict is None:
                        raise RuntimeError("CoC session history capacity exhausted")
                    del known[evict]
                known[session] = 0
                self._state["current"] = session
                self._state["gap"] = True
            self._generation = _new_generation(self.address)
            self._session = session
        send_frame(self._data, encode_packet(READY, session, 0, b"\x20\x02"))
        send_frame(self._control, encode_packet(READY, session, 0, b"\x20\x02"))
        for sock in (self._data, self._control):
            sock.settimeout(None)
        self._started = True
        self._threads = [
            threading.Thread(target=self._read_records, name="CoC-records", daemon=True),
            threading.Thread(target=self._read_control, name="CoC-control", daemon=True),
            threading.Thread(target=self._write_control, name="CoC-control-writer", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _generation_active(self) -> bool:
        return not self._closed.is_set() and _ACTIVE_GENERATIONS.get(self.address) == self._generation

    def _queue_control(self, kind: int, ident: int, payload: bytes = b"", *, reserved: bool = False) -> bool:
        if kind == RECEIPT:
            if not reserved and not self._receipt_slots.acquire(blocking=False):
                return False
            priority = 1
            slot = "receipt"
        else:
            if not self._command_slots.acquire(blocking=False):
                return False
            priority = 0
            slot = "command"
        try:
            packet = encode_packet(kind, self._session, ident, payload)
            with self._outbound_lock:
                self._outbound_sequence += 1
                order = self._outbound_sequence
            self._outbound.put_nowait((priority, order, packet, slot))
            return True
        except (queue.Full, ValueError):
            (self._receipt_slots if slot == "receipt" else self._command_slots).release()
            return False

    def _write_control(self) -> None:
        while not self._closed.is_set():
            try:
                _priority, _order, packet, slot = self._outbound.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                _version, kind, _session, ident = HEADER.unpack_from(packet)
                if kind == COMMAND:
                    with self._pending_lock:
                        pending = self._pending.get(ident)
                        if pending is None or time.monotonic() >= pending.deadline:
                            continue
                        # Linearization boundary: after this claim the write is
                        # in flight and cannot be recalled by a caller timeout.
                        pending.claimed = True
                send_frame(self._control, packet)
            except (OSError, ValueError):
                self._mark_disconnected()
            finally:
                (self._receipt_slots if slot == "receipt" else self._command_slots).release()
                self._outbound.task_done()

    def _read_records(self) -> None:
        try:
            while self._generation_active():
                packet = decode_packet(recv_frame(self._data), "host")
                if packet.kind != RECORD or packet.session != self._session:
                    raise ValueError("unexpected CoC data SDU")
                state = self._state
                assert state is not None
                admitted = False
                receipt_slot = False
                with _STATE_LOCK:
                    if not self._generation_active() or packet.session != state["current"]:
                        return
                    previous = state["sessions"][packet.session]
                    if packet.ident <= previous:
                        self._metrics["duplicates"] += 1
                        receipt_slot = self._receipt_slots.acquire(blocking=False)
                    elif len(state["lines"]) >= state["queue_limit"]:
                        state["gap"] = True
                        self._metrics["admission_drops"] += 1
                        continue
                    elif not self._receipt_slots.acquire(blocking=False):
                        state["gap"] = True
                        self._metrics["admission_drops"] += 1
                        continue
                    else:
                        gap = state["gap"] or (previous > 0 and packet.ident != previous + 1)
                        line = packet.payload.decode("utf-8", "replace").rstrip("\r\n")
                        state["lines"].append((packet.session, line, gap))
                        state["sessions"][packet.session] = packet.ident
                        state["gap"] = False
                        admitted = True
                        receipt_slot = True
                # Duplicate receipts are safe: that exact ID was previously admitted.
                # New records are receipted only after bounded host-queue admission.
                if receipt_slot:
                    self._queue_control(RECEIPT, packet.ident, reserved=True)
                if admitted:
                    self._last_activity = time.time()
        except (OSError, EOFError, ValueError, AssertionError):
            self._mark_disconnected()

    def _read_control(self) -> None:
        try:
            while self._generation_active():
                packet = decode_packet(recv_frame(self._control), "host")
                if packet.session != self._session or packet.kind not in (REPLY, STATS_REPLY):
                    raise ValueError("unexpected CoC control SDU")
                with self._pending_lock:
                    pending = self._pending.get(packet.ident)
                    if pending is not None:
                        pending.response.append(packet.payload.decode("utf-8", "replace"))
                        pending.event.set()
        except (OSError, EOFError, ValueError):
            self._mark_disconnected()

    def _mark_disconnected(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            for sock in (self._data, self._control):
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass

    @staticmethod
    def _extract_response(command: str, response: str) -> str:
        text = response.strip()
        if "-> >" in text:
            text = text.split("-> >", 1)[1].strip()
        elif "-> " in text:
            text = text.split("-> ", 1)[1].strip()
        if text.startswith(">"):
            text = text[1:].strip()
        if text.startswith(command):
            text = text[len(command):].strip()
        text = text.rstrip("> ").strip()
        return text or "(no output)"

    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]:
        if not isinstance(command, str) or not (command in READ_COMMANDS or self._canonical_time(command)):
            return False, "Command not allowed"
        if not self.is_open:
            return False, "CoC disconnected"
        with self._pending_lock:
            if self._next_id >= 0xFFFFFFFF:
                return False, "CoC request ID capacity exhausted"
            if len(self._pending) >= MAX_PENDING_COMMANDS:
                return False, "CoC command capacity exhausted"
            self._next_id += 1
            ident = self._next_id
            pending = _PendingCommand(threading.Event(), [], time.monotonic() + max(0.0, timeout))
            self._pending[ident] = pending
            # Firmware requires increasing request IDs. Allocation and admission
            # must be one critical section across all concurrent callers.
            queued = self._queue_control(COMMAND, ident, command.encode("utf-8"))
        try:
            if not queued:
                return False, "CoC control queue full"
            if not pending.event.wait(max(0.0, pending.deadline - time.monotonic())):
                return False, "Command timeout"
            return True, self._extract_response(command, pending.response[0])
        except (OSError, ValueError):
            return False, "CoC command transport failed"
        finally:
            with self._pending_lock:
                self._pending.pop(ident, None)

    @staticmethod
    def _canonical_time(command: str) -> bool:
        match = re.fullmatch(r"time (0|[1-9][0-9]{0,9})", command)
        return bool(match) and int(command[5:]) <= 0xFFFFFFFF

    def set_time(self) -> None:
        ok, response = self.execute_command(f"time {int(time.time())}")
        if not ok or not (response == "OK - clock set" or response.startswith("OK - clock set:")):
            raise OSError("CoC clock synchronization was not acknowledged")

    def _get(self, command: str) -> str | None:
        ok, response = self.execute_command(command)
        return response if ok else None

    def get_name(self) -> str | None: return self._get("get name")
    def get_pubkey(self) -> str | None:
        response = self._get("get public.key")
        key = response.replace(" ", "") if response else ""
        return key.upper() if len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key) else None

    def get_privkey(self) -> str | None:
        response = self._get("get prv.key")
        key = response.replace(" ", "") if response else ""
        return key if len(key) == 128 and all(c in "0123456789abcdefABCDEF" for c in key) else None
    def get_radio_info(self) -> str | None: return self._get("get radio")
    def get_firmware_version(self) -> str | None: return self._get("ver")
    def get_board_type(self) -> str | None: return self._get("board")

    def get_device_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        fields = {
            "stats-core": {"battery_mv": "battery_mv", "uptime_secs": "uptime_secs", "errors": "debug_flags", "queue_len": "queue_len"},
            "stats-radio": {"noise_floor": "noise_floor", "tx_air_secs": "tx_air_secs", "rx_air_secs": "rx_air_secs"},
            "stats-packets": {"recv_errors": "recv_errors"},
        }
        for command, mapping in fields.items():
            response = self._get(command)
            if not response:
                continue
            try:
                text = response.split("-> ", 1)[1].strip() if "-> " in response else response.strip()
                parsed = json.loads(text.splitlines()[0])
                if isinstance(parsed, dict):
                    for source, target in mapping.items():
                        if source in parsed:
                            stats[target] = parsed[source]
            except (ValueError, IndexError, TypeError):
                continue
        return stats

    def read_line(self) -> str | None:
        with _STATE_LOCK:
            if self._state is None or not self._state["lines"]:
                return None
            _session, line, gap = self._state["lines"].popleft()
            self.record_gap_before_line = gap
            return line

    def seconds_since_activity(self) -> float:
        return time.time() - self._last_activity

    def close(self) -> None:
        with self._close_lock:
            self._mark_disconnected()
            with _STATE_LOCK:
                if _ACTIVE_GENERATIONS.get(self.address) == self._generation:
                    _ACTIVE_GENERATIONS.pop(self.address, None)
            for sock in (self._data, self._control):
                try: sock.close()
                except OSError: pass
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=.5)
            _stop_process(self._proc)

    @property
    def is_open(self) -> bool:
        return self._started and self._generation_active()


def _connect_unix_until(path: str, proc: subprocess.Popen, deadline: float) -> socket.socket:
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("CoC helper exited before IPC startup")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(.01, min(.2, deadline - time.monotonic())))
            sock.connect(path)
            return sock
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
            sock.close()
            time.sleep(.025)
    raise TimeoutError("CoC helper IPC startup timed out")


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.wait(timeout=.4)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try: proc.wait(timeout=.5)
        except subprocess.TimeoutExpired: pass


def _helper_failure_category(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, EOFError):
        return "ipc_eof"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, ValueError):
        version_match = re.fullmatch(r"unsupported CoC protocol version ([0-9]{1,3})", str(error))
        if version_match:
            return f"unsupported_version_{version_match.group(1)}"
        safe_values = {
            "invalid CoC SDU size": "invalid_sdu_size",
            "zero CoC session": "zero_session",
            "invalid CoC packet direction": "wrong_direction",
            "invalid CoC kind": "invalid_kind",
            "invalid CoC packet id": "invalid_packet_id",
            "unexpected CoC payload": "unexpected_payload",
            "invalid CoC handshake": "invalid_handshake",
            "invalid CoC payload length": "invalid_payload_length",
            "invalid CoC channel HELLOs": "hello_mismatch",
            "CoC HELLO session did not match expected session": "hello_session_mismatch",
            "stale CoC session rejected": "stale_session",
        }
        return safe_values.get(str(error), "invalid_value")
    if isinstance(error, RuntimeError):
        return "runtime"
    if isinstance(error, OSError):
        return "os_error"
    if isinstance(error, subprocess.SubprocessError):
        return "subprocess"
    return "other"


def connect(config: dict[str, Any]) -> CocConnection | None:
    try:
        return CocConnection.launch(config)
    except (OSError, EOFError, RuntimeError, subprocess.SubprocessError, TimeoutError, ValueError, PermissionError) as error:
        # Exception text can contain paths, peer data, or backend details; emit
        # only its fixed Python category alongside the helper's fixed-stage log.
        logger.warning("CoC helper unavailable or rejected peer (%s); no transport fallback", _helper_failure_category(error))
        return None
