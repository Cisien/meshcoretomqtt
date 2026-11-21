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

from config_loader import log_config_sources, resolve_transport

from . import serial_connection
from . import coc_connection
from . import message_parser
from . import background
from .auth_provider import MeshCoreAuthProvider
from .mqtt_publish import publish_status

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)


def process_device_line(state: BridgeState) -> bool:
    """Pass one accepted classic line to the unchanged parser."""
    line = state.device.read_line()
    if not line:
        return False
    if getattr(state.device, "record_gap_before_line", False):
        state.last_raw = None
    # CoC also carries key replies; never dump its unclassified stream to logs.
    if not isinstance(state.device, coc_connection.CocConnection):
        logger.debug(f"RX: {line}")
    message_parser.parse_and_publish(state, line)
    return True


def connect_transport(config: dict[str, Any], *, serial_factory=None, coc_factory=None):
    """Connect the explicitly selected device transport; serial is the default."""
    try:
        transport = resolve_transport(config)
    except ValueError as exc:
        logger.error("Invalid transport configuration: %s", exc)
        return None
    if transport == 'serial':
        return (serial_connection.connect if serial_factory is None else serial_factory)(config)
    return (coc_connection.connect if coc_factory is None else coc_factory)(config)


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


def wait_for_system_time_sync(state: BridgeState, *, require_sync: bool = False,
                              status_reader=None, retry_wait=None) -> bool:
    """Wait up to 60 seconds for system clock synchronization via timedatectl."""
    attempts = 0
    while attempts < 60 and not state.should_exit:
        try:
            if status_reader is None:
                command = (['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']
                           if require_sync else ['timedatectl', 'status'])
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            else:
                result = status_reader()
        except FileNotFoundError:
            logger.warning("timedatectl not found — skipping sync check and continuing.")
            return not require_sync
        except Exception as e:
            logger.warning("Error checking time sync (%s). Continuing.", e)
            return not require_sync

        if ((require_sync and result.returncode == 0 and result.stdout.strip() == 'yes')
                or (not require_sync and "System clock synchronized: yes" in result.stdout)):
            return True
        logger.warning("System clock is not synchronized: %s",
                       result.stderr.strip() or result.stdout.strip())
        attempts += 1
        if retry_wait is None:
            time.sleep(1)
        else:
            retry_wait()

    logger.warning("Timed out waiting for system clock sync — continuing anyway.")
    return not require_sync


def _connect_device(state: BridgeState, transport_factory, *, time_sync_wait=None):
    """Restore CoC device time after startup or radio reconnect, when enabled."""
    state.device = transport_factory(state.config)
    if (state.device and resolve_transport(state.config) == "coc"
            and state.sync_time_at_start and not state.should_exit):
        synchronized = (wait_for_system_time_sync(state, require_sync=True)
                        if time_sync_wait is None else time_sync_wait(state))
        if synchronized and not state.should_exit:
            try:
                state.device.set_time()
                logger.info("CoC repeater clock synchronized to host UTC")
            except OSError:
                logger.warning("CoC repeater clock synchronization failed; retry on next connection")
        else:
            logger.warning("CoC repeater clock unchanged: host UTC not verified or stopping")
    return state.device


def run(state: BridgeState, *, transport_factory=None) -> None:
    """Main orchestration: connect serial, query device, connect MQTT, read loop."""
    stats_thread = threading.Thread(
        target=background.stats_logging_loop,
        args=(state,),
        daemon=True,
        name="Stats-Logger",
    )
    # Startup owns a device before MQTT can be established. Keep its resource
    # lifetime inside the same finally as the main loop: an early return must
    # not leave a BlueZ connection alive after this process exits.
    try:
        _run(state, stats_thread,
             connect_transport if transport_factory is None else transport_factory)
    finally:
        _cleanup(state, stats_thread)


def _run(state: BridgeState, stats_thread: threading.Thread, transport_factory) -> None:
    """Run startup and packet processing within the caller's cleanup scope."""
    log_config_sources(state.config)

    # Connect selected transport (serial remains the backwards-compatible default).
    state.device = _connect_device(state, transport_factory)
    if not state.device:
        return

    # Set up auth provider
    state.auth = MeshCoreAuthProvider()

    # Time sync
    if state.sync_time_at_start and resolve_transport(state.config) != "coc":
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
    while retry_count < max_initial_retries and not state.should_exit:
        if state.mqtt_manager.connect_all_brokers():
            break
        else:
            if state.should_exit:
                return
            retry_count += 1
            wait_time = min(retry_count * 2, 30)
            logger.warning(f"[MQTT] Initial connection failed. Retrying in {wait_time}s... (attempt {retry_count}/{max_initial_retries})")
            sleep(wait_time)

    if state.should_exit:
        return

    if retry_count >= max_initial_retries:
        logger.error("[MQTT] Failed to establish initial connection after maximum retries")
        state.should_exit = True
        return

    # Start stats logging thread
    stats_thread.start()
    logger.debug("[STATS] Started statistics logging thread")

    # Watchdog: force reconnect if no data received for this many seconds.
    transport_name = resolve_transport(state.config)
    transport_label = "CoC" if transport_name == "coc" else "Serial"
    transport_cfg = state.config.get(transport_name, {})
    watchdog_timeout = transport_cfg.get('watchdog_timeout', 900)
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
                    if not state.device.is_open:
                        logger.warning("Device transport disconnected, reconnecting")
                        state.device.close()
                        state.device = _connect_device(state, transport_factory)
                        sleep(0.5)
                        continue
                    if process_device_line(state):
                        watchdog_logged = False

                    # Watchdog: detect silently dead serial connections
                    elif state.device.seconds_since_activity() > watchdog_timeout:
                        if not watchdog_logged:
                            logger.warning(
                                f"{transport_label} watchdog: no data received for "
                                f"{int(state.device.seconds_since_activity())}s "
                                f"(threshold: {watchdog_timeout}s), forcing reconnect"
                            )
                        state.device.close()
                        state.device = _connect_device(state, transport_factory)
                        if state.device:
                            logger.info("%s watchdog: reconnected successfully", transport_label)
                            watchdog_logged = False
                        else:
                            watchdog_logged = True
                        sleep(0.5)
                else:
                    # Device is None — periodically retry connection
                    now = time.time()
                    if now - last_reconnect_attempt >= reconnect_interval:
                        last_reconnect_attempt = now
                        state.device = _connect_device(state, transport_factory)
                        if state.device:
                            logger.info("%s reconnected successfully", transport_label)
                            watchdog_logged = False
                        else:
                            if not watchdog_logged:
                                logger.warning("%s device unavailable, retrying every %ds", transport_label, reconnect_interval)
                                watchdog_logged = True

            except OSError:
                logger.warning("%s connection unavailable, trying to reconnect", transport_label)
                if state.device:
                    state.device.close()
                state.device = _connect_device(state, transport_factory)
                sleep(0.5)

            sleep(0.01)

    except KeyboardInterrupt:
        logger.info("\nExiting...")
    except Exception as e:
        logger.exception(f"Unhandled error in main loop: {e}")


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
