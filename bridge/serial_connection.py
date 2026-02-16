"""Serial connection abstraction for MeshCore device communication."""
from __future__ import annotations

import calendar
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from time import sleep
from typing import Any

import serial

logger = logging.getLogger(__name__)


class SerialConnection(ABC):
    """Abstract interface for MeshCore device communication.

    Implementations own their own locking — callers never manage threading.
    Methods are getters that return parsed values; no external state mutation.
    """

    @abstractmethod
    def set_time(self) -> None: ...

    @abstractmethod
    def get_name(self) -> str | None: ...

    @abstractmethod
    def get_pubkey(self) -> str | None: ...

    @abstractmethod
    def get_privkey(self) -> str | None: ...

    @abstractmethod
    def get_radio_info(self) -> str | None: ...

    @abstractmethod
    def get_firmware_version(self) -> str | None: ...

    @abstractmethod
    def get_board_type(self) -> str | None: ...

    @abstractmethod
    def get_device_stats(self) -> dict[str, Any]: ...

    @abstractmethod
    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]: ...

    @abstractmethod
    def read_line(self) -> str | None:
        """Non-blocking read of next available line, or None if nothing waiting."""
        ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...


class RealSerialConnection(SerialConnection):
    """Concrete implementation wrapping serial.Serial with internal locking."""

    def __init__(self, port: serial.Serial) -> None:
        self._port = port
        self._lock = threading.Lock()

    def _send(self, cmd: str, delay: float = 0.5) -> str:
        """Send command and read response under lock."""
        with self._lock:
            return self._send_unlocked(cmd, delay)

    def _send_unlocked(self, cmd: str, delay: float = 0.5) -> str:
        """Send command and read response (caller must hold lock)."""
        self._port.reset_input_buffer()
        self._port.reset_output_buffer()
        self._port.write(cmd.encode())
        sleep(delay)
        return self._port.read_all().decode(errors='replace')

    def set_time(self) -> None:
        epoch_time = int(calendar.timegm(time.gmtime()))
        cmd = f'time {epoch_time}\r\n'
        response = self._send(cmd)
        logger.debug(f"Set time response: {response}")

    def get_name(self) -> str | None:
        response = self._send("get name\r\n")
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            name = response.split("-> >")[1].strip()
            if '\n' in name:
                name = name.split('\n')[0]
            name = name.replace('\r', '').strip()
            logger.info(f"Repeater name: {name}")
            return name

        logger.error("Failed to get repeater name from response")
        return None

    def get_pubkey(self) -> str | None:
        response = self._send("get public.key\r\n", delay=1.0)
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            pub_key = response.split("-> >")[1].strip()
            if '\n' in pub_key:
                pub_key = pub_key.split('\n')[0]
            pub_key_clean = pub_key.replace(' ', '').replace('\r', '').replace('\n', '')

            if not pub_key_clean or len(pub_key_clean) != 64 or not all(c in '0123456789ABCDEFabcdef' for c in pub_key_clean):
                logger.error(f"Invalid public key format: {repr(pub_key_clean)} (extracted from: {repr(pub_key)})")
                return None

            result = pub_key_clean.upper()
            logger.info(f"Repeater pub key: {result}")
            return result

        logger.error("Failed to get repeater pub key from response")
        return None

    def get_privkey(self) -> str | None:
        response = self._send("get prv.key\r\n", delay=1.0)

        if "-> >" in response:
            priv_key = response.split("-> >")[1].strip()
            if '\n' in priv_key:
                priv_key = priv_key.split('\n')[0]

            priv_key_clean = priv_key.replace(' ', '').replace('\r', '').replace('\n', '')
            if len(priv_key_clean) == 128:
                try:
                    int(priv_key_clean, 16)
                    logger.info(f"Repeater priv key: {priv_key_clean[:4]}... (truncated for security)")
                    return priv_key_clean
                except ValueError as e:
                    logger.error(f"Response not valid hex: {priv_key_clean[:32]}... Error: {e}")
            else:
                logger.error(f"Response wrong length: {len(priv_key_clean)} (expected 128)")

        logger.error("Failed to get repeater priv key from response - command may not be supported by firmware")
        return None

    def get_radio_info(self) -> str | None:
        response = self._send("get radio\r\n")
        logger.debug(f"Raw radio response: {response}")

        if "-> >" in response:
            radio_info = response.split("-> >")[1].strip()
            if '\n' in radio_info:
                radio_info = radio_info.split('\n')[0]
            logger.debug(f"Parsed radio info: {radio_info}")
            return radio_info

        logger.error("Failed to get radio info from response")
        return None

    def get_firmware_version(self) -> str | None:
        response = self._send("ver\r\n")
        logger.debug(f"Raw version response: {response}")

        if "-> " in response:
            version = response.split("-> ", 1)[1]
            version = version.split('\n')[0].replace('\r', '').strip()
            logger.info(f"Firmware version: {version}")
            return version

        logger.warning("Failed to get firmware version from response")
        return None

    def get_board_type(self) -> str | None:
        response = self._send("board\r\n")
        logger.debug(f"Raw board response: {response}")

        if "-> " in response:
            board_type = response.split("-> ", 1)[1]
            board_type = board_type.split('\n')[0].replace('\r', '').strip()
            if board_type == "Unknown command":
                board_type = "unknown"
            logger.info(f"Board type: {board_type}")
            return board_type

        logger.warning("Failed to get board type from response")
        return None

    def get_device_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}

        with self._lock:
            # stats-core: battery_mv, uptime_secs, errors, queue_len
            response = self._send_unlocked("stats-core\r\n")
            logger.debug(f"Raw stats-core response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    core_stats = json.loads(json_str)
                    if 'battery_mv' in core_stats:
                        stats['battery_mv'] = core_stats['battery_mv']
                    if 'uptime_secs' in core_stats:
                        stats['uptime_secs'] = core_stats['uptime_secs']
                    if 'errors' in core_stats:
                        stats['debug_flags'] = core_stats['errors']
                    if 'queue_len' in core_stats:
                        stats['queue_len'] = core_stats['queue_len']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-core: {e}")

            # stats-radio: noise_floor, tx_air_secs, rx_air_secs
            response = self._send_unlocked("stats-radio\r\n")
            logger.debug(f"Raw stats-radio response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    radio_stats = json.loads(json_str)
                    if 'noise_floor' in radio_stats:
                        stats['noise_floor'] = radio_stats['noise_floor']
                    if 'tx_air_secs' in radio_stats:
                        stats['tx_air_secs'] = radio_stats['tx_air_secs']
                    if 'rx_air_secs' in radio_stats:
                        stats['rx_air_secs'] = radio_stats['rx_air_secs']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-radio: {e}")

            # stats-packets: recv_errors
            response = self._send_unlocked("stats-packets\r\n")
            logger.debug(f"Raw stats-packets response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    packets_stats: dict[str, Any] = json.loads(json_str)
                    if 'recv_errors' in packets_stats:
                        stats['recv_errors'] = packets_stats['recv_errors']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-packets: {e}")

        return stats

    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]:
        try:
            with self._lock:
                self._port.reset_input_buffer()
                self._port.reset_output_buffer()

                cmd_bytes = command.strip()
                if not cmd_bytes.endswith('\r\n'):
                    cmd_bytes += '\r\n'

                self._port.write(cmd_bytes.encode('utf-8'))
                logger.debug(f"[SERIAL] Sent: {command.strip()}")

                start_time = time.time()
                response_lines: list[str] = []

                while (time.time() - start_time) < timeout:
                    sleep(0.1)

                    if self._port.in_waiting > 0:
                        data = self._port.read_all().decode(errors='replace')
                        response_lines.append(data)

                        full_response = ''.join(response_lines)
                        if '-> ' in full_response or full_response.rstrip().endswith('>'):
                            break

                full_response = ''.join(response_lines)

                if "-> >" in full_response:
                    response_text = full_response.split("-> >")[1].strip()
                elif "-> " in full_response:
                    response_text = full_response.split("-> ", 1)[1].strip()
                elif "> " in full_response:
                    response_text = full_response.split("> ", 1)[1].strip()
                else:
                    response_text = full_response.strip()

                if response_text.startswith(command.strip()):
                    response_text = response_text[len(command.strip()):].strip()

                response_text = response_text.rstrip('> ').strip()

                if not response_text:
                    response_text = "(no output)"

                logger.debug(f"[SERIAL] Response: {response_text[:100]}{'...' if len(response_text) > 100 else ''}")
                return True, response_text

        except serial.SerialException as e:
            logger.error(f"[SERIAL] Serial error executing command: {e}")
            return False, f"Serial error: {str(e)}"
        except Exception as e:
            logger.error(f"[SERIAL] Error executing command: {e}")
            return False, f"Error: {str(e)}"

    def read_line(self) -> str | None:
        with self._lock:
            if self._port.in_waiting > 0:
                line = self._port.readline().decode(errors='replace').strip()
                return line if line else None
        return None

    def close(self) -> None:
        try:
            with self._lock:
                if self._port and getattr(self._port, 'is_open', False):
                    logger.debug("Closing serial connection")
                    self._port.close()
        except Exception:
            pass

    @property
    def is_open(self) -> bool:
        return getattr(self._port, 'is_open', False)


def connect(config: dict[str, Any]) -> RealSerialConnection | None:
    """Try configured serial ports and return the first successful connection."""
    serial_cfg = config.get('serial', {})
    ports = serial_cfg.get('ports', ['/dev/ttyACM0'])
    baud_rate = serial_cfg.get('baud_rate', 115200)
    timeout = serial_cfg.get('timeout', 2)

    for port in ports:
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud_rate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=timeout,
                rtscts=False
            )
            ser.write(b"\r\n\r\n")
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            logger.info(f"Connected to {port}")
            return RealSerialConnection(ser)
        except (serial.SerialException, OSError) as e:
            logger.warning(f"Failed to connect to {port}: {str(e)}")
            continue

    logger.error("Failed to connect to any serial port")
    return None
