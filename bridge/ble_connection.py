"""Bonded BlueZ/Bleak transport for the SenseCAP observer GATT service.

The firmware sends each *complete classic serial line* as one or more framed
notifications. Frames are reassembled record-atomically: offset-zero retries
replace partial bytes; nonzero duplicate/reordered fragments drop the partial
record rather than joining it to a later line.
"""
from __future__ import annotations

import asyncio
import calendar
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import logging
import re
import struct
import threading
import time
from collections import deque
from typing import Any, Callable

from .serial_connection import SerialConnection

try:  # Imported lazily in tests through client_factory; required at runtime.
    from bleak import BleakClient
except ImportError:  # pragma: no cover - exercised only without the extra installed
    BleakClient = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Bluefruit's BLEUuid(uint8_t[16]) receives little-endian bytes. These are its
# toString() values for the exact arrays in ObserverTransport.cpp.
SERVICE_UUID = "10c25d4a-3c61-50a3-5e48-566b026f5f1b"
LOG_CHARACTERISTIC_UUID = "10c25d4a-3c61-50a3-5e48-566b036f5f1b"
COMMAND_CHARACTERISTIC_UUID = "10c25d4a-3c61-50a3-5e48-566b046f5f1b"
ACK_CHARACTERISTIC_UUID = "10c25d4a-3c61-50a3-5e48-566b056f5f1b"
STATS_CHARACTERISTIC_UUID = "10c25d4a-3c61-50a3-5e48-566b066f5f1b"
FRAME_HEADER_BYTES = 16
MAX_RECORD_BYTES = 544  # firmware LineBuffer capacity
GATT_TIMEOUT = 2.0
MAX_SESSION_HISTORY = 64
MAX_PEERS = 64

READ_COMMANDS = frozenset({
    "get name", "get public.key", "get prv.key", "get radio", "ver", "board",
    "stats-core", "stats-radio", "stats-packets",
})


class BleSecurityError(RuntimeError):
    """BlueZ did not report the provisioned bonded/trusted peer."""


class BleProtocolError(RuntimeError):
    """The discovered GATT service does not match the observer firmware."""


class BleConnection(SerialConnection):
    """Classic host connection interface carried by the observer GATT service."""

    # Stop-and-wait firmware assigns monotonically increasing sequences. A high
    # water mark deduplicates an entire boot without retaining record payloads.
    # Retain active high-water marks across reconnects. Retired history may age
    # out ONLY at a fresh connection boundary, after old callbacks are fenced.
    _process_sessions: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        address: str,
        *,
        service_uuid: str = SERVICE_UUID,
        client_factory: Callable[[str], Any] | None = None,
        peer_properties: dict[str, Any] | None = None,
        max_queued_lines: int = 256,
    ) -> None:
        self.address = address
        self.service_uuid = service_uuid.lower()
        self._client_factory = client_factory
        self._peer_properties = peer_properties
        self._client: Any | None = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._transaction_lock = threading.RLock()
        self._active_command: str | None = None
        self._active_response: str | None = None
        self._response_event = threading.Event()
        self._response_desynced = False
        self._connection_generation = 0
        self._retirement_reconnect_required = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        if max_queued_lines < 1:
            raise ValueError("max_queued_lines must be positive")
        self._lines: deque[tuple[str, bool]] = deque(maxlen=max_queued_lines)
        self._record_id: tuple[int, int] | None = None
        self._write_lock: asyncio.Lock | None = None
        self._completion_lock: asyncio.Lock | None = None
        self._pending_completions = 0
        self._pending_gap = False
        self._queue_loss = False
        self.record_gap_before_line = False
        self._record_length = 0
        self._record_offset = 0
        self._record_bytes = bytearray()
        self._last_activity = time.time()
        self.metrics = {
            "malformed_frames": 0,
            "reordered_frames": 0,
            "incomplete_records": 0,
            "dropped_records": 0,
            "disconnects": 0,
            "duplicate_records": 0,
            "stale_sessions": 0,
            "sequence_gaps": 0,
            "ack_failures": 0,
            "acked_records": 0,
            "session_capacity_errors": 0,
            "stats_read_failures": 0,
        }

    def _loop_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                # Bound cancellation cleanup too: a backend coroutine that
                # suppresses cancellation must not pin close()/the owner thread.
                loop.run_until_complete(asyncio.wait(pending, timeout=0.1))
                for task in asyncio.all_tasks(loop):
                    task.cancel()
            loop.close()

    def _start_loop(self) -> None:
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._loop_ready.clear()
        self._loop_thread = threading.Thread(target=self._loop_main, name="Bleak-Event-Loop", daemon=True)
        self._loop_thread.start()
        if not self._loop_ready.wait(timeout=5) or self._loop is None:
            raise RuntimeError("BLE event loop failed to start")

    async def _create_client(self) -> Any:
        """Construct the Bleak backend on its owning event-loop thread."""
        self._write_lock = asyncio.Lock()
        self._completion_lock = asyncio.Lock()
        self._retirement_reconnect_required = False
        if self._client_factory is not None:
            return self._client_factory(self.address)
        if BleakClient is None:  # pragma: no cover - requires deliberately missing dependency
            raise RuntimeError("Bleak is required for BLE transport")
        return BleakClient(self.address)

    def _run(self, awaitable: Any, *, timeout: float | None = None) -> Any:
        """Submit Bleak work to this connection's persistent event-loop owner."""
        loop = self._loop
        if loop is None or not loop.is_running():
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise RuntimeError("BLE event loop is not running")
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise

    def _stop_loop(self) -> None:
        loop, thread = self._loop, self._loop_thread
        self._loop = None
        self._loop_thread = None
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    @staticmethod
    def _canonical_time(command: str) -> bool:
        if not command.startswith("time "):
            return False
        digits = command[5:]
        return bool(re.fullmatch(r"0|[1-9][0-9]{0,9}", digits)) and int(digits) <= 0xFFFFFFFF

    def _allowed(self, command: str) -> bool:
        return command in READ_COMMANDS or self._canonical_time(command)

    def _client_properties(self) -> dict[str, Any]:
        if self._peer_properties is not None:
            return self._peer_properties
        # BleakClient wraps the platform backend (observed with Bleak 3.0.2).
        # BlueZ stores Device1 properties on that backend, not the wrapper.
        # Direct backend factories remain supported; a present but invalid
        # backend must not fall back to unrelated wrapper properties.
        backend = getattr(self._client, "_backend", self._client)
        properties = getattr(backend, "_device_info", None)
        return properties if isinstance(properties, dict) else {}

    def _validate_security(self) -> None:
        properties = self._client_properties()
        if properties.get("Paired") is not True or properties.get("Trusted") is not True:
            raise BleSecurityError("observer BLE peer must be paired and trusted in BlueZ")

    def _validate_service(self) -> None:
        services = getattr(self._client, "services", None)
        if services is None:
            raise BleProtocolError("observer GATT services were not discovered")
        if isinstance(services, (set, list, tuple)):
            known = {str(item).lower() for item in services}
            required = {self.service_uuid, LOG_CHARACTERISTIC_UUID, COMMAND_CHARACTERISTIC_UUID, ACK_CHARACTERISTIC_UUID, STATS_CHARACTERISTIC_UUID}
            if required.issubset(known):
                return
            raise BleProtocolError("SenseCAP observer GATT service or characteristic missing")
        service = services.get_service(self.service_uuid) if hasattr(services, "get_service") else None
        characteristics = getattr(service, "characteristics", ()) if service else ()
        known = {str(getattr(item, "uuid", item)).lower() for item in characteristics}
        if service is None or not {LOG_CHARACTERISTIC_UUID, COMMAND_CHARACTERISTIC_UUID, ACK_CHARACTERISTIC_UUID, STATS_CHARACTERISTIC_UUID}.issubset(known):
            raise BleProtocolError("SenseCAP observer GATT service or characteristic missing")

    def open(self) -> None:
        """Connect, validate the BlueZ bond state, and subscribe to observer logs."""
        with self._lifecycle_lock:
            if self._retirement_reconnect_required and self._client and getattr(self._client, "is_connected", False):
                raise BleProtocolError("close BLE link before retiring session history")
            if self._client and getattr(self._client, "is_connected", False) and (
                self._loop is None or not self._loop.is_running()
            ):
                raise RuntimeError(
                    "previous BLE client is still connected; refusing a second link"
                )
            if self.is_open:
                return
            try:
                self._start_loop()
                self._client = self._run(self._create_client())
                self._run(self._client.connect())
                self._validate_security()
                self._validate_service()
                with self._lock:
                    self._connection_generation += 1
                    generation = self._connection_generation
                    state = self._process_sessions.get(self.address.upper())
                    if state and len(state["sessions"]) >= MAX_SESSION_HISTORY:
                        # A new secured peer connection cannot deliver callbacks
                        # from retired links. Keep the active boot/high-water mark
                        # and the newest retired boots, freeing one admission slot.
                        oldest = next(nonce for nonce in state["sessions"] if nonce != state["current"])
                        del state["sessions"][oldest]
                callback = lambda sender, data: self._on_notification(
                    sender, data, generation=generation
                )
                self._run(self._client.start_notify(LOG_CHARACTERISTIC_UUID, callback))
            except Exception:
                try:
                    if self._client and self.is_open:
                        self._run(self._client.disconnect())
                finally:
                    self._stop_loop()
                raise
            self._response_desynced = False

    def _drop_record(self, *, incomplete: bool = False, dropped: bool = True) -> None:
        if self._record_id is not None:
            if dropped:
                self.metrics["dropped_records"] += 1
            if incomplete:
                self.metrics["incomplete_records"] += 1
        self._record_id = None
        self._record_length = 0
        self._record_offset = 0
        self._record_bytes.clear()

    def _on_notification(
        self, _sender: Any, data: bytearray, *, generation: int | None = None
    ) -> None:
        frame = bytes(data)
        with self._lock:
            if generation is not None and generation != self._connection_generation:
                return
            if self._retirement_reconnect_required:
                return
            if len(frame) < FRAME_HEADER_BYTES:
                self.metrics["malformed_frames"] += 1
                self._drop_record(incomplete=True)
                return
            nonce, sequence, total, offset = struct.unpack_from("<QIHH", frame)
            record_id = (nonce, sequence)
            state = self._process_sessions.get(self.address.upper())
            if state and nonce != state["current"] and nonce in state["sessions"]:
                self.metrics["stale_sessions"] += 1
                return
            payload = frame[FRAME_HEADER_BYTES:]
            length = len(payload)
            if not nonce or not sequence or not total or total > MAX_RECORD_BYTES or length == 0 or offset + length > total:
                self.metrics["malformed_frames"] += 1
                self._drop_record(incomplete=True)
                return
            if self._record_id is None:
                if offset != 0:
                    self.metrics["malformed_frames"] += 1
                    return
                self._record_id, self._record_length, self._record_offset = record_id, total, 0
            elif record_id != self._record_id:
                self._drop_record(incomplete=True)
                if offset != 0:
                    self.metrics["malformed_frames"] += 1
                    return
                self._record_id, self._record_length, self._record_offset = record_id, total, 0
            elif offset == 0 and total == self._record_length:
                # Firmware retries the whole head record after a failed notify
                # or ACK timeout. Never splice old partial bytes into a retry;
                # require its full ordered contents before ACK/deduplication.
                self._record_bytes.clear()
                self._record_offset = 0
            if record_id != self._record_id or total != self._record_length or offset != self._record_offset:
                self.metrics["reordered_frames"] += 1
                self._drop_record()
                return
            self._record_bytes.extend(payload)
            self._record_offset += length
            if self._record_offset != self._record_length:
                return
            line = self._record_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            self._drop_record(dropped=False)
            if self._pending_completions >= self._lines.maxlen:
                self.metrics["dropped_records"] += 1
                self._pending_gap = True
                return
            self._pending_completions += 1
            generation = self._connection_generation
        completion = self._accept_record(nonce, sequence, line, generation)
        if threading.current_thread() is self._loop_thread:
            asyncio.create_task(completion)
        else:
            self._run(completion, timeout=GATT_TIMEOUT + 0.5)

    async def _write(self, uuid: str, value: bytes) -> None:
        # Serialize only the GATT operation, NEVER the wait for a framed command
        # reply: that reply itself needs an ACK on the same link.
        async with self._write_lock:
            await asyncio.wait_for(self._client.write_gatt_char(uuid, value, response=True), GATT_TIMEOUT)

    async def _accept_record(self, nonce: int, sequence: int, line: str, generation: int) -> None:
        try:
            async with self._completion_lock:
                if generation != self._connection_generation or self._retirement_reconnect_required:
                    return
                key = self.address.upper()
                state = self._process_sessions.get(key)
                if state is None:
                    if len(self._process_sessions) >= MAX_PEERS:
                        self.metrics["session_capacity_errors"] += 1
                        return
                    state = {"current": None, "sessions": {}}
                    self._process_sessions[key] = state
                sessions = state["sessions"]
                if state.pop("delivery_gap", False):
                    self._pending_gap = True
                if nonce != state["current"]:
                    if nonce in sessions:
                        self.metrics["stale_sessions"] += 1
                        return
                    if len(sessions) >= MAX_SESSION_HISTORY:
                        self.metrics["session_capacity_errors"] += 1
                        # Let runner perform its existing bounded teardown and
                        # reconnect; never evict history on a live generation.
                        self._retirement_reconnect_required = True
                        return
                    state["current"] = nonce
                    sessions[nonce] = 0
                    self._pending_gap = True
                previous = sessions[nonce]
                try:
                    await self._write(ACK_CHARACTERISTIC_UUID, f"ack {nonce:016x} {sequence}".encode("ascii"))
                except Exception:
                    # Do not log backend exceptions: they can echo private-key
                    # command replies or other sensitive characteristic values.
                    self.metrics["ack_failures"] += 1
                    return
                with self._lock:
                    if generation != self._connection_generation:
                        return
                    self.metrics["acked_records"] += 1
                    self._last_activity = time.time()
                    if sequence <= previous:
                        self.metrics["duplicate_records"] += 1
                        return
                    if previous and sequence != previous + 1:
                        self.metrics["sequence_gaps"] += sequence - previous - 1
                        self._pending_gap = True
                    sessions[nonce] = sequence
                    if self._active_command != "get prv.key" and self._response_matches("get prv.key", line):
                        # A firmware-retained key reply can precede setup after
                        # reconnect. ACK it, but never send orphaned key material
                        # into the classic parser's debug logging path.
                        pass
                    elif self._active_command is not None and self._response_matches(self._active_command, line):
                        self._active_response = line
                        self._response_event.set()
                    elif line:
                        if len(self._lines) == self._lines.maxlen:
                            self.metrics["dropped_records"] += 1
                            self._queue_loss = True
                        self._lines.append((line, self._pending_gap))
                        self._pending_gap = False
        finally:
            self._pending_completions -= 1

    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]:
        if not self._allowed(command):
            return False, "observer operation not allowed"
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._transaction_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False, "BLE command timed out waiting for transaction"
        try:
            with self._lock:
                if not self.is_open:
                    return False, "BLE connection is not open"
                if self._response_desynced:
                    return False, "BLE response stream requires reconnect after timeout"
                self._active_command = command
                self._active_response = None
                self._response_event.clear()
            try:
                remaining = max(0.0, deadline - time.monotonic())
                self._run(
                    self._write(COMMAND_CHARACTERISTIC_UUID, command.encode()),
                    timeout=remaining,
                )
            except FutureTimeoutError:
                with self._lock:
                    self._active_command = None
                    self._active_response = None
                    self._response_desynced = True
                self._close_link(timeout=0.25)
                return False, "BLE command timed out; reconnect required to resynchronize replies"
            except Exception:
                with self._lock:
                    self._active_command = None
                    self._active_response = None
                    self._response_desynced = True
                logger.warning("BLE observer command failed; reconnect required")
                self._close_link(timeout=0.25)
                return False, "BLE command failed; reconnect required"
            if not self._response_event.wait(timeout=max(0.0, deadline - time.monotonic())):
                with self._lock:
                    self._active_command = None
                    self._active_response = None
                    self._response_desynced = True
                # The wire protocol has no transaction ID, so a late reply
                # cannot safely be distinguished from the next command. Drop
                # this link; runner reconnects and starts from a clean stream.
                self._close_link()
                return False, "BLE command timed out; reconnect required to resynchronize replies"
            with self._lock:
                response = self._active_response
                self._active_command = None
                self._active_response = None
            return True, self._clean_response(command, response or "")
        finally:
            self._transaction_lock.release()

    @staticmethod
    def _response_matches(command: str, line: str) -> bool:
        """Match only a plausible reply for an active command, not log/prompt lines."""
        value = line.strip()
        if value.startswith("->"):
            value = value[2:].strip()
        if not value or value.endswith(">") or value.startswith("DEBUG"):
            return False
        if " RAW:" in value or re.search(r" U: (?:RX|TX), len=", value):
            return False
        if command.startswith("time "):
            return value.startswith(("OK - clock set", "(ERR:", "ERR:"))
        if value == "Unknown command":
            return command == "board"
        if value.startswith(("ERR:", "(ERR:")):
            return False
        if command in {"stats-core", "stats-radio", "stats-packets"}:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return False
            expected = {
                "stats-core": {"battery_mv", "uptime_secs", "errors", "queue_len"},
                "stats-radio": {"noise_floor", "tx_air_secs", "rx_air_secs"},
                "stats-packets": {"recv_errors"},
            }[command]
            return isinstance(parsed, dict) and bool(expected.intersection(parsed))
        if command == "get name":
            return value.startswith(">") and len(value) > 1
        if command == "get radio":
            return bool(re.fullmatch(r">\s*[^,\s]+,[^,\s]+,\d+,\d+", value))
        if command == "ver":
            return bool(re.fullmatch(r"[^\r\n]+\(Build: [^\r\n]+\)", value))
        if command == "board":
            return value not in {"(no output)", "Unknown command"} and not value.startswith((">", "ERR:", "(ERR:"))
        if command in {"get public.key", "get prv.key"}:
            size = 64 if command == "get public.key" else 128
            key = value.removeprefix(">").replace(" ", "")
            return len(key) == size and all(char in "0123456789abcdefABCDEF" for char in key)
        return True

    @staticmethod
    def _clean_response(command: str, response: str) -> str:
        if response.startswith("-> >"):
            result = response[4:].strip().removeprefix(">")
        elif response.startswith("->"):
            result = response[2:].strip()
        else:
            result = response.strip()
        if result.startswith(">"):
            result = result[1:].strip()
        if result.startswith(command):
            result = result[len(command):].strip()
        return result or "(no output)"

    def _request(self, command: str) -> str | None:
        success, response = self.execute_command(command)
        return response if success else None

    def set_time(self) -> None:
        self.execute_command(f"time {calendar.timegm(time.gmtime())}")

    def get_name(self) -> str | None:
        return self._request("get name")

    def get_pubkey(self) -> str | None:
        response = self._request("get public.key")
        key = response.replace(" ", "") if response else ""
        return key.upper() if len(key) == 64 and all(char in "0123456789abcdefABCDEF" for char in key) else None

    def get_privkey(self) -> str | None:
        response = self._request("get prv.key")
        key = response.replace(" ", "") if response else ""
        try:
            int(key, 16)
        except ValueError:
            return None
        return key if len(key) == 128 else None

    def get_radio_info(self) -> str | None:
        return self._request("get radio")

    def get_firmware_version(self) -> str | None:
        return self._request("ver")

    def get_board_type(self) -> str | None:
        response = self._request("board")
        return "unknown" if response == "Unknown command" else response

    def get_device_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        fields = {
            "stats-core": {"battery_mv": "battery_mv", "uptime_secs": "uptime_secs", "errors": "debug_flags", "queue_len": "queue_len"},
            "stats-radio": {"noise_floor": "noise_floor", "tx_air_secs": "tx_air_secs", "rx_air_secs": "rx_air_secs"},
            "stats-packets": {"recv_errors": "recv_errors"},
        }
        for command, names in fields.items():
            response = self._request(command)
            if not response or response == "Unknown command":
                continue
            try:
                parsed = json.loads(response)
            except (TypeError, json.JSONDecodeError):
                continue
            for source, target in names.items():
                if source in parsed:
                    stats[target] = parsed[source]
        if stats:
            self._last_activity = time.time()
        try:
            async def read_counters() -> bytes:
                async with self._write_lock:
                    return await asyncio.wait_for(self._client.read_gatt_char(STATS_CHARACTERISTIC_UUID), GATT_TIMEOUT)
            raw = self._run(read_counters(), timeout=GATT_TIMEOUT + 0.5)
            match = re.fullmatch(rb"evicted=(\d+)/(\d+) truncated=(\d+)/(\d+) failed=(\d+)(?: unavailable=(\d+)/(\d+))?", raw)
            values = [int(value) for value in match.groups() if value is not None] if match else []
            if not match or any(value > 0xFFFFFFFF for value in values):
                raise BleProtocolError("invalid observer counters")
            names = ("evicted_records", "evicted_bytes", "truncated_records", "truncated_bytes", "failed_notifications", "unavailable_records", "unavailable_bytes")
            stats["observer_firmware"] = dict(zip(names, values))
        except Exception:
            self.metrics["stats_read_failures"] += 1
        stats["observer_host"] = dict(self.metrics)
        return stats

    def read_line(self) -> str | None:
        with self._lock:
            if not self._lines:
                return None
            line, gap = self._lines.popleft()
            self.record_gap_before_line = gap or self._queue_loss
            self._queue_loss = False
            return line

    def seconds_since_activity(self) -> float:
        return time.time() - self._last_activity

    def close(self) -> None:
        with self._transaction_lock:
            with self._lifecycle_lock:
                self._close_link()

    def _close_link(self, *, timeout: float = 1.0) -> None:
        if not self._client:
            return
        client = self._client
        operation_timeout = max(0.01, timeout)
        # Invalidate callbacks before teardown begins; a callback already queued
        # by the old client must not append data to a later connection.
        with self._lock:
            self._connection_generation += 1
            state = self._process_sessions.get(self.address.upper())
            if state is not None and self._lines:
                state["delivery_gap"] = True
        try:
            if self._loop and self._loop.is_running():
                was_connected = bool(getattr(client, "is_connected", False))
                async def teardown() -> bool:
                    # Give each operation its own budget. A timeout/cancellation
                    # in stop_notify must not consume disconnect's opportunity.
                    async def stop_notifications() -> None:
                        if getattr(client, "is_connected", False):
                            await client.stop_notify(LOG_CHARACTERISTIC_UUID)

                    async def disconnect_client() -> None:
                        # A remote drop can leave backend resources alive.
                        # Bleak 3.0.2 still owns its D-Bus connection until this
                        # public cleanup runs, even when is_connected is false.
                        await client.disconnect()

                    stop_task = asyncio.create_task(stop_notifications())
                    disconnect_task = None
                    try:
                        await asyncio.wait({stop_task}, timeout=operation_timeout)
                        if not stop_task.done():
                            stop_task.cancel()

                        disconnect_task = asyncio.create_task(disconnect_client())
                        done, _ = await asyncio.wait(
                            {disconnect_task}, timeout=operation_timeout
                        )
                        disconnected = bool(done) and not disconnect_task.cancelled()
                        if not disconnect_task.done():
                            disconnect_task.cancel()

                        # Allow cooperative cancellations to finish before the
                        # event loop is stopped; resistant tasks stay bounded by
                        # this grace wait and the caller's outer timeout.
                        tasks = {stop_task, disconnect_task}
                        pending = {task for task in tasks if not task.done()}
                        if pending:
                            done, _ = await asyncio.wait(pending, timeout=0.05)
                        else:
                            done = tasks
                        for task in done:
                            if not task.cancelled():
                                try:
                                    task.exception()
                                except Exception:
                                    pass
                        disconnect_error = (
                            disconnect_task.exception()
                            if disconnect_task.done() and not disconnect_task.cancelled()
                            else None
                        )
                        return disconnected and disconnect_error is None
                    except Exception:
                        return False
                    finally:
                        for task in (stop_task, disconnect_task):
                            if task is not None and not task.done():
                                task.cancel()

                if self._run(teardown(), timeout=(2 * operation_timeout) + 0.2) and was_connected:
                    self.metrics["disconnects"] += 1
        except Exception:
            pass
        finally:
            with self._lock:
                self._drop_record(incomplete=True)
                self._active_command = None
                self._active_response = None
                self._response_event.set()
            self._stop_loop()

    @property
    def is_open(self) -> bool:
        # A cached backend connection flag is not a usable transport after
        # bounded teardown stops its owner loop. Keep that flag untouched so
        # open() still refuses a second link while disconnect is unconfirmed.
        return bool(
            self._loop and self._loop.is_running()
            and self._client and getattr(self._client, "is_connected", False)
            and not self._retirement_reconnect_required
        )


def connect(config: dict[str, Any], *, client_factory=None) -> BleConnection | None:
    """Open explicitly configured BLE transport; serial remains the default."""
    ble = config.get("ble", {})
    address = ble.get("address")
    if not address:
        logger.error("BLE transport requires ble.address")
        return None
    connection = BleConnection(address, service_uuid=ble.get("service_uuid", SERVICE_UUID),
                               client_factory=client_factory)
    try:
        connection.open()
        return connection
    except Exception as exc:
        logger.warning("BLE observer unavailable: %s", exc)
        connection.close()
        return None
