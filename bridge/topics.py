"""Topic resolution and broker config helpers."""
from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import BridgeState


def get_broker_config(state: BridgeState, broker_idx: int) -> dict[str, Any]:
    """Get broker config by index into the broker list."""
    brokers = state.config.get('broker', [])
    if broker_idx < len(brokers):
        return brokers[broker_idx]
    return {}


def resolve_topic_template(state: BridgeState, template: str, broker_idx: int | None = None) -> str:
    """Resolve topic template with {IATA} and {PUBLIC_KEY} placeholders."""
    if not template:
        return template

    iata = state.global_iata
    if broker_idx is not None:
        broker = get_broker_config(state, broker_idx)
        broker_topics = broker.get('topics', {})
        broker_iata = broker_topics.get('iata', '')
        if broker_iata:
            iata = broker_iata

    resolved = template.replace('{IATA}', iata)
    resolved = resolved.replace('{PUBLIC_KEY}', state.repeater_pub_key if state.repeater_pub_key else 'UNKNOWN')
    return resolved


def get_topic(state: BridgeState, topic_type: str, broker_idx: int | None = None) -> str:
    """Get topic with template resolution, checking broker-specific override first."""
    if broker_idx is not None:
        broker = get_broker_config(state, broker_idx)
        broker_topics = broker.get('topics', {})
        broker_topic = broker_topics.get(topic_type, '')
        if broker_topic:
            return resolve_topic_template(state, broker_topic, broker_idx)

    topics = state.config.get('topics', {})
    global_topic = topics.get(topic_type, '')
    return resolve_topic_template(state, global_topic, broker_idx)


def sanitize_client_id(name: str, prefix: str = "meshcore_") -> str:
    """Convert a name to a valid MQTT client ID."""
    client_id = prefix + name.replace(" ", "_")
    client_id = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)
    return client_id[:23]
