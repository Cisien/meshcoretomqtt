"""MQTT publishing helpers."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING

from . import topics

if TYPE_CHECKING:
    from .state import BridgeState
    from .broker_client import BrokerClient

logger = logging.getLogger(__name__)


def safe_publish(
    state: BridgeState,
    topic: str,
    payload: str,
    retain: bool = False,
    client: BrokerClient | None = None,
    broker_idx: int | None = None,
) -> bool:
    """Publish to one or all MQTT brokers."""
    if not state.mqtt_connected:
        logger.warning(f"Not connected - skipping publish to {topic}")
        state.stats['publish_failures'] += 1
        return False

    success = False

    if client:
        clients_to_publish = [info for info in state.mqtt_clients if info['client'] is client]
    else:
        clients_to_publish = state.mqtt_clients

    for mqtt_client_info in clients_to_publish:
        bidx = mqtt_client_info['broker_idx']
        broker = topics.get_broker_config(state, bidx)
        broker_name = broker.get('name', f'broker-{bidx}')
        try:
            broker_client = mqtt_client_info['client']
            qos = broker.get('qos', 0)
            if qos == 1:
                qos = 0  # force qos=1 to 0 because qos 1 can cause retry storms

            result = broker_client.publish(topic, payload, qos=qos, retain=retain)
            if not result:
                logger.error(f"[{broker_name}] Publish failed to {topic}")
                state.stats['publish_failures'] += 1
            else:
                logger.debug(f"[{broker_name}] Published to {topic}")
                success = True
        except Exception as e:
            logger.error(f"[{broker_name}] Publish error to {topic}: {str(e)}")
            state.stats['publish_failures'] += 1

    return success


def build_status_message(state: BridgeState, status: str, include_stats: bool = True) -> dict[str, Any]:
    """Build a status message with all required fields."""
    message: dict[str, Any] = {
        "status": status,
        "timestamp": datetime.now().isoformat(),
        "origin": state.repeater_name,
        "origin_id": state.repeater_pub_key,
        "radio": state.radio_info if state.radio_info else "unknown",
        "model": state.model if state.model else "unknown",
        "firmware_version": state.firmware_version if state.firmware_version else "unknown",
        "client_version": state.client_version
    }

    if include_stats and state.stats['device']:
        message['stats'] = state.stats['device']

    return message


def publish_status(
    state: BridgeState,
    status: str,
    client: BrokerClient | None = None,
    broker_idx: int | None = None,
) -> None:
    """Publish status message (NOT retained)."""
    status_msg = build_status_message(state, status, include_stats=True)
    status_topic = topics.get_topic(state, "status", broker_idx)

    if client:
        safe_publish(state, status_topic, json.dumps(status_msg), retain=False, client=client, broker_idx=broker_idx)
    else:
        safe_publish(state, status_topic, json.dumps(status_msg), retain=False)

    logger.debug(f"Published status: {status}")
