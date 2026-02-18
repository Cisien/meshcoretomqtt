"""Background thread loops for stats logging and WebSocket keepalive."""
from __future__ import annotations

import logging
import time
from time import sleep
from typing import Any, TYPE_CHECKING

from . import topics
from .mqtt_publish import publish_status

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)


def stats_logging_loop(state: BridgeState) -> None:
    """Log statistics every 5 minutes."""
    stats_interval = 300

    while not state.should_exit:
        sleep(stats_interval)

        if state.should_exit:
            break

        # Fetch fresh device stats from serial
        logger.debug("[STATS] Fetching fresh device stats from serial...")
        if state.device:
            device_stats = state.device.get_device_stats()
            if device_stats:
                state.stats['device'] = device_stats
                logger.debug(f"[STATS] Updated device stats: {device_stats}")
                publish_status(state, "online")
            else:
                logger.debug("[STATS] No device stats received")

        # Calculate uptime
        uptime_seconds = int(time.time() - state.stats['start_time'])
        uptime_hours = uptime_seconds // 3600
        uptime_minutes = (uptime_seconds % 3600) // 60

        if uptime_hours > 0:
            uptime_str = f"{uptime_hours}h {uptime_minutes}m"
        else:
            uptime_str = f"{uptime_minutes}m"

        # Calculate data volume with appropriate units
        bytes_actual = state.stats['bytes_processed']
        if bytes_actual < 1024:
            data_str = f"{bytes_actual}B"
        elif bytes_actual < 1024 * 1024:
            data_str = f"{bytes_actual / 1024:.1f}KB"
        elif bytes_actual < 1024 * 1024 * 1024:
            data_str = f"{bytes_actual / (1024 * 1024):.1f}MB"
        else:
            data_str = f"{bytes_actual / (1024 * 1024 * 1024):.2f}GB"

        total_brokers = len(state.mqtt_clients)
        connected_brokers = sum(1 for info in state.mqtt_clients if info.get('connected', False))

        # Calculate packets per minute over the last interval
        time_elapsed = time.time() - state.stats['last_stats_log']
        packets_rx_delta = state.stats['packets_rx'] - state.stats['packets_rx_prev']
        packets_tx_delta = state.stats['packets_tx'] - state.stats['packets_tx_prev']
        packets_per_min = ((packets_rx_delta + packets_tx_delta) / time_elapsed) * 60 if time_elapsed > 0 else 0

        state.stats['packets_rx_prev'] = state.stats['packets_rx']
        state.stats['packets_tx_prev'] = state.stats['packets_tx']

        # Prune reconnect timestamps older than 24 hours
        current_time = time.time()
        cutoff_time = current_time - 86400
        reconnect_stats: list[str] = []

        for broker_idx in sorted(state.stats['reconnects'].keys()):
            state.stats['reconnects'][broker_idx] = [
                ts for ts in state.stats['reconnects'][broker_idx] if ts > cutoff_time
            ]
            reconnect_count = len(state.stats['reconnects'][broker_idx])
            if reconnect_count > 0:
                broker = topics.get_broker_config(state, broker_idx)
                name = broker.get('name', f'broker-{broker_idx}')
                reconnect_stats.append(f"{name}:{reconnect_count}")

        reconnect_str = ", ".join(reconnect_stats) if reconnect_stats else "none"

        logger.info(
            f"[SERVICE] Uptime: {uptime_str} | "
            f"RX/TX: {state.stats['packets_rx']}/{state.stats['packets_tx']} (5m: {packets_per_min:.1f}/min) | "
            f"RX bytes: {data_str} | "
            f"MQTT: {connected_brokers}/{total_brokers} | "
            f"Reconnects/24h: {reconnect_str} | "
            f"Failures: {state.stats['publish_failures']}"
        )

        # Log device stats separately if available
        if state.stats['device']:
            _log_device_stats(state, time_elapsed)

        # Save current device stats as previous for next interval
        if state.stats['device']:
            state.stats['device_prev'] = state.stats['device'].copy()

        state.stats['last_stats_log'] = time.time()


def _log_device_stats(state: BridgeState, time_elapsed: float) -> None:
    """Format and log device statistics."""
    ds = state.stats['device']
    parts: list[str] = []

    if 'noise_floor' in ds:
        parts.append(f"Noise: {ds['noise_floor']}dB")

    # Radio airtime stats with utilization
    if 'tx_air_secs' in ds and 'rx_air_secs' in ds and 'uptime_secs' in ds:
        tx_secs_total = ds['tx_air_secs']
        rx_secs_total = ds['rx_air_secs']
        uptime_secs = ds['uptime_secs']

        prev = state.stats.get('device_prev', {})
        if prev and 'tx_air_secs' in prev and 'rx_air_secs' in prev and 'uptime_secs' in prev:
            tx_delta = tx_secs_total - prev['tx_air_secs']
            rx_delta = rx_secs_total - prev['rx_air_secs']
            uptime_delta = uptime_secs - prev['uptime_secs']

            if uptime_delta > 0:
                tx_util = (tx_delta / uptime_delta) * 100
                rx_util = (rx_delta / uptime_delta) * 100
                parts.append(f"Air (5m): Tx {tx_delta:.1f}s ({tx_util:.2f}%), Rx {rx_delta:.1f}s ({rx_util:.2f}%)")
            else:
                parts.append(f"Air (5m): Tx {tx_delta:.1f}s, Rx {rx_delta:.1f}s")
        else:
            parts.append(f"Air (5m): Tx {tx_secs_total}s, Rx {rx_secs_total}s")
    elif 'tx_air_secs' in ds and 'rx_air_secs' in ds:
        parts.append(f"Air (5m): Tx {ds['tx_air_secs']}s, Rx {ds['rx_air_secs']}s")

    if 'battery_mv' in ds:
        parts.append(f"Battery: {ds['battery_mv']}mV")

    if 'uptime_secs' in ds:
        dev_uptime_secs = ds['uptime_secs']
        dev_uptime_hours = dev_uptime_secs // 3600
        dev_uptime_minutes = (dev_uptime_secs % 3600) // 60

        if dev_uptime_hours > 0:
            dev_uptime_str = f"{dev_uptime_hours}h {dev_uptime_minutes}m"
        else:
            dev_uptime_str = f"{dev_uptime_minutes}m"

        parts.append(f"Uptime: {dev_uptime_str}")

    if 'debug_flags' in ds:
        parts.append(f"Debug Flags: {ds['debug_flags']}")

    if 'queue_len' in ds:
        parts.append(f"Queue: {ds['queue_len']}")

    if 'recv_errors' in ds:
        prev = state.stats.get('device_prev', {})
        prev_errors = prev.get('recv_errors', 0) if prev else 0
        errors_delta = ds['recv_errors'] - prev_errors
        errors_per_min = (errors_delta / time_elapsed) * 60 if time_elapsed > 0 else 0
        parts.append(f"Err/min (5m): {errors_per_min:.1f}")

    if parts:
        logger.info(f"[DEVICE] {' | '.join(parts)}")


def websocket_ping_loop(state: BridgeState, broker_idx: int, broker_client: Any, transport: str) -> None:
    """Send WebSocket PING frames periodically to keep connection alive."""
    if transport != "websockets":
        return

    ping_interval = 45

    while broker_idx in state.ws_ping_threads and state.ws_ping_threads[broker_idx].get('active', False):
        sleep(ping_interval)

        try:
            raw_client = broker_client.raw_client if hasattr(broker_client, 'raw_client') else None
            if raw_client and hasattr(raw_client, '_sock') and raw_client._sock:
                sock = raw_client._sock
                if hasattr(sock, 'ping'):
                    sock.ping()
                    logger.debug(f"[{broker_idx}] Sent WebSocket PING")
        except Exception as e:
            logger.debug(f"[{broker_idx}] WebSocket PING failed: {e}")
