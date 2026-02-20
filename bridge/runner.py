"""Main run loop and startup orchestration."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from time import sleep
from typing import Any, TYPE_CHECKING

from config_loader import log_config_sources

from . import serial_connection
from . import message_parser
from . import background
from .auth_provider import MeshCoreAuthProvider
from .mqtt_publish import publish_status

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)


def load_client_version(version: str) -> str:
    """Load client version from provided version string, optionally append git hash."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(script_dir)  # bridge/ → project root
        version_file = os.path.join(parent_dir, '.version_info')
        if os.path.exists(version_file):
            with open(version_file, 'r') as f:
                version_data = json.load(f)
                git_hash = version_data.get('git_hash', '')
                if git_hash and git_hash != 'unknown':
                    return f"meshcoretomqtt/{version}-{git_hash}"
    except Exception as e:
        logger.debug(f"Could not load version info: {e}")
    return f"meshcoretomqtt/{version}"


def handle_signal(state: BridgeState, signum: int, frame: Any) -> None:
    """Signal handler to trigger graceful shutdown."""
    logger.info(f"Received signal {signum}, shutting down...")
    state.should_exit = True


def wait_for_system_time_sync(state: BridgeState) -> bool:
    """Wait up to 60 seconds for system clock synchronization via timedatectl."""
    attempts = 0
    while attempts < 60 and not state.should_exit:
        try:
            result = subprocess.run(
                ['timedatectl', 'status'],
                capture_output=True,
                text=True,
                timeout=10
            )
        except FileNotFoundError:
            logger.warning("timedatectl not found — skipping sync check and continuing.")
            return True
        except Exception as e:
            logger.warning("Error checking time sync (%s). Continuing.", e)
            return True

        if "System clock synchronized: yes" in result.stdout:
            return True
        logger.warning("System clock is not synchronized: %s",
                       result.stderr.strip() or result.stdout.strip())
        attempts += 1
        time.sleep(1)

    logger.warning("Timed out waiting for system clock sync — continuing anyway.")
    return True


def run(state: BridgeState) -> None:
    """Main orchestration: connect serial, query device, connect MQTT, read loop."""
    log_config_sources(state.config)

    # Connect serial
    state.device = serial_connection.connect(state.config)
    if not state.device:
        return

    # Set up auth provider
    state.auth = MeshCoreAuthProvider()

    # Time sync
    if state.sync_time_at_start:
        wait_for_system_time_sync(state)
        state.device.set_time()

    # Query device info
    state.repeater_name = state.device.get_name()
    if not state.repeater_name:
        logger.error("Failed to get repeater name")
        return

    state.repeater_pub_key = state.device.get_pubkey()
    if not state.repeater_pub_key:
        logger.error("Failed to get the repeater id (public key)")
        return

    state.repeater_priv_key = state.device.get_privkey()
    if not state.repeater_priv_key:
        logger.warning("Failed to get repeater private key - auth token authentication will not be available")

    state.radio_info = state.device.get_radio_info()
    if not state.radio_info:
        logger.error("Failed to get radio info")
        return

    state.firmware_version = state.device.get_firmware_version()
    if not state.firmware_version:
        logger.warning("Failed to get firmware version - will continue without it")

    state.model = state.device.get_board_type()
    if not state.model:
        logger.warning("Failed to get board type - will continue without it")

    # Get initial device stats
    device_stats = state.device.get_device_stats()
    if device_stats:
        state.stats['device'] = device_stats
        state.stats['device_prev'] = device_stats.copy()
        logger.info(f"Device stats: {device_stats}")
    else:
        logger.debug("Device stats not available (firmware may not support stats commands)")

    logger.info(f"Client version: {state.client_version}")

    # Log remote serial configuration
    if state.remote_serial_enabled:
        if state.remote_serial_allowed_companions:
            logger.info(f"Remote serial: ENABLED ({len(state.remote_serial_allowed_companions)} companion(s) allowed)")
            for pubkey in sorted(state.remote_serial_allowed_companions):
                logger.debug(f"  Allowed companion: {pubkey[:16]}...")
        else:
            logger.warning("Remote serial: ENABLED but no companions configured (will reject all commands)")
        if state.remote_serial_disallowed_commands:
            logger.info(f"Remote serial blocked commands: {state.remote_serial_disallowed_commands}")
    else:
        logger.info("Remote serial: DISABLED")

    # Initial MQTT connection
    retry_count = 0
    max_initial_retries = 10
    while retry_count < max_initial_retries:
        if state.mqtt_manager.connect_all_brokers():
            break
        else:
            retry_count += 1
            wait_time = min(retry_count * 2, 30)
            logger.warning(f"[MQTT] Initial connection failed. Retrying in {wait_time}s... (attempt {retry_count}/{max_initial_retries})")
            sleep(wait_time)

    if retry_count >= max_initial_retries:
        logger.error("[MQTT] Failed to establish initial connection after maximum retries")
        state.should_exit = True
        return

    # Start stats logging thread
    stats_thread = threading.Thread(
        target=background.stats_logging_loop,
        args=(state,),
        daemon=True,
        name="Stats-Logger"
    )
    stats_thread.start()
    logger.debug("[STATS] Started statistics logging thread")

    # Serial watchdog: force reconnect if no data received for this many seconds
    serial_cfg = state.config.get('serial', {})
    watchdog_timeout = serial_cfg.get('watchdog_timeout', 900)
    watchdog_logged = False
    last_reconnect_attempt = 0.0
    reconnect_interval = 5  # seconds between retry attempts

    # Main event loop
    try:
        while True:
            if state.should_exit:
                break

            state.mqtt_manager.reconnect_disconnected_brokers()

            try:
                if state.device:
                    line = state.device.read_line()
                    if line:
                        logger.debug(f"RX: {line}")
                        message_parser.parse_and_publish(state, line)
                        watchdog_logged = False

                    # Watchdog: detect silently dead serial connections
                    elif state.device.seconds_since_activity() > watchdog_timeout:
                        if not watchdog_logged:
                            logger.warning(
                                f"Serial watchdog: no data received for "
                                f"{int(state.device.seconds_since_activity())}s "
                                f"(threshold: {watchdog_timeout}s), forcing reconnect"
                            )
                        state.device.close()
                        state.device = serial_connection.connect(state.config)
                        if state.device:
                            logger.info("Serial watchdog: reconnected successfully")
                            watchdog_logged = False
                        else:
                            watchdog_logged = True
                        sleep(0.5)
                else:
                    # Device is None — periodically retry connection
                    now = time.time()
                    if now - last_reconnect_attempt >= reconnect_interval:
                        last_reconnect_attempt = now
                        state.device = serial_connection.connect(state.config)
                        if state.device:
                            logger.info("Serial reconnected successfully")
                            watchdog_logged = False
                        else:
                            if not watchdog_logged:
                                logger.warning("Serial device unavailable, retrying every %ds", reconnect_interval)
                                watchdog_logged = True

            except OSError:
                logger.warning("Serial connection unavailable, trying to reconnect")
                if state.device:
                    state.device.close()
                state.device = serial_connection.connect(state.config)
                sleep(0.5)

            sleep(0.01)

    except KeyboardInterrupt:
        logger.info("\nExiting...")
    except Exception as e:
        logger.exception(f"Unhandled error in main loop: {e}")
    finally:
        _cleanup(state, stats_thread)


def _cleanup(state: BridgeState, stats_thread: threading.Thread) -> None:
    """Shut down background threads, publish offline status, and close connections."""
    logger.info("Cleaning up...")
    state.should_exit = True

    # Stop WebSocket ping threads
    if state.mqtt_manager:
        for mqtt_info in list(state.mqtt_clients):
            broker_idx = mqtt_info.get('broker_idx')
            if broker_idx is not None:
                state.mqtt_manager.stop_websocket_ping_thread(broker_idx)

    # Wait for stats thread to finish
    if stats_thread.is_alive():
        stats_thread.join(timeout=5)

    # Publish offline status before disconnecting
    for mqtt_info in state.mqtt_clients:
        if mqtt_info.get('connected'):
            try:
                publish_status(state, "offline",
                               client=mqtt_info['client'],
                               broker_idx=mqtt_info['broker_idx'])
            except Exception:
                pass

    # Disconnect MQTT clients
    for mqtt_info in state.mqtt_clients:
        try:
            mqtt_info['client'].loop_stop()
            mqtt_info['client'].disconnect()
        except Exception:
            pass

    # Close serial connection
    if state.device:
        state.device.close()
