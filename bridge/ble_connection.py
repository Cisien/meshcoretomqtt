"""BLE serial connection for receiving MeshCore packet logs via Nordic UART Service."""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any

from .serial_connection import SerialConnection

logger = logging.getLogger(__name__)

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


class BLESerialConnection(SerialConnection):
    """Receive MeshCore packet-log lines via BLE Nordic UART Service (NUS).

    The BLE log stream is one-way (device → client): the device sends log lines
    as NUS TX notifications.  Bidirectional commands (get name, set time, etc.)
    are not supported; device identity values must be provided in config.

    The async BLE loop runs in a background thread so callers stay synchronous.
    The connection auto-reconnects on drop; the main-loop watchdog handles the
    case where the device disappears entirely.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._lines: queue.Queue[str] = queue.Queue()
        self._partial = ""
        self._last_activity = time.time()
        self._should_stop = False
        self._ble_connected = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_ble_loop, daemon=True, name="BLE-Loop"
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Background BLE loop
    # ------------------------------------------------------------------

    def _run_ble_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ble_task = self._loop.create_task(self._ble_loop())
        try:
            self._loop.run_until_complete(self._ble_task)
        except asyncio.CancelledError:
            pass

    async def _scan_for_device(self) -> str | None:
        from bleak import BleakScanner  # type: ignore[import]

        ble_cfg = self._config.get('ble', {})
        scan_name: str | None = ble_cfg.get('scan_name')
        timeout = float(ble_cfg.get('scan_timeout', 10))

        logger.info("BLE: scanning for NUS device (name=%r, timeout=%.0fs)...", scan_name, timeout)

        def match(device: Any, adv: Any) -> bool:
            if scan_name and device.name != scan_name:
                return False
            return NUS_SERVICE_UUID in [str(s).lower() for s in adv.service_uuids]

        device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
        if device:
            logger.info("BLE: found %r at %s", device.name, device.address)
            return device.address
        return None

    async def _ble_loop(self) -> None:
        from bleak import BleakClient  # type: ignore[import]

        ble_cfg = self._config.get('ble', {})
        address: str = ble_cfg.get('address', 'scan')

        if address == 'scan':
            address = await self._scan_for_device() or ''
            if not address:
                logger.error("BLE: no device found during scan — giving up")
                return

        while not self._should_stop:
            try:
                async with BleakClient(address, timeout=10.0) as client:
                    self._ble_connected = True
                    logger.info("BLE: connected to %s", address)
                    await client.start_notify(NUS_TX_UUID, self._on_notification)
                    while client.is_connected and not self._should_stop:
                        await asyncio.sleep(0.1)
                    self._ble_connected = False
                    if not self._should_stop:
                        logger.info("BLE: disconnected, reconnecting in 5s...")
                        await asyncio.sleep(5)
            except Exception as exc:
                self._ble_connected = False
                if not self._should_stop:
                    logger.warning("BLE: connection error (%s), retrying in 5s", exc)
                    await asyncio.sleep(5)

    def _on_notification(self, handle: Any, data: bytearray) -> None:
        """Called from the BLE asyncio thread for each incoming NUS notification."""
        self._partial += data.decode('utf-8', errors='replace')
        while '\n' in self._partial:
            line, self._partial = self._partial.split('\n', 1)
            line = line.rstrip('\r')
            if line:
                self._lines.put(line)
                self._last_activity = time.time()

    # ------------------------------------------------------------------
    # SerialConnection interface — read side
    # ------------------------------------------------------------------

    def read_line(self) -> str | None:
        try:
            return self._lines.get_nowait()
        except queue.Empty:
            return None

    def seconds_since_activity(self) -> float:
        return time.time() - self._last_activity

    def close(self) -> None:
        self._should_stop = True
        self._ble_connected = False
        if not self._loop.is_closed() and hasattr(self, '_ble_task'):
            self._loop.call_soon_threadsafe(self._ble_task.cancel)
        self._thread.join(timeout=5)

    @property
    def is_open(self) -> bool:
        return self._thread.is_alive() and not self._should_stop

    # ------------------------------------------------------------------
    # SerialConnection interface — device info from config
    # ------------------------------------------------------------------

    def set_time(self) -> None:
        pass  # Cannot send commands over one-way BLE log stream

    def get_name(self) -> str | None:
        return self._config.get('ble', {}).get('device_name') or None

    def get_pubkey(self) -> str | None:
        key: str = self._config.get('ble', {}).get('public_key', '')
        return key.upper() if key else None

    def get_privkey(self) -> str | None:
        return None  # Not available over BLE

    def get_radio_info(self) -> str | None:
        return self._config.get('ble', {}).get('radio_info') or None

    def get_firmware_version(self) -> str | None:
        return None

    def get_board_type(self) -> str | None:
        return None

    def get_device_stats(self) -> dict[str, Any]:
        return {}

    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]:
        logger.warning("BLE: execute_command not supported over one-way log stream (%r)", command)
        return False, "Not supported over BLE log stream"
