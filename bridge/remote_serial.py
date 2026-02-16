"""Remote serial command handling via MQTT."""
from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

from . import topics
from .mqtt_publish import safe_publish

if TYPE_CHECKING:
    from .state import BridgeState
    from .broker_client import BrokerClient

logger = logging.getLogger(__name__)


def is_command_allowed(state: BridgeState, command: str) -> tuple[bool, str | None]:
    """Check if a command is allowed (not in disallowed list)."""
    cmd_lower = command.strip().lower()

    for disallowed in state.remote_serial_disallowed_commands:
        if cmd_lower.startswith(disallowed.lower()):
            return False, disallowed

    return True, None


def cleanup_old_nonces(state: BridgeState) -> None:
    """Remove expired nonces from the tracking dict."""
    current_time = int(time.time())
    cutoff_time = current_time - state.remote_serial_nonce_ttl

    expired = [nonce for nonce, ts in state.remote_serial_nonces.items() if ts < cutoff_time]
    for nonce in expired:
        del state.remote_serial_nonces[nonce]

    if expired:
        logger.debug(f"[SERIAL] Cleaned up {len(expired)} expired nonces")


def subscribe_serial_commands(state: BridgeState, client: BrokerClient, broker_idx: int) -> None:
    """Subscribe to the serial/commands topic for this node."""
    if not state.remote_serial_enabled:
        return

    if not state.repeater_pub_key:
        broker = topics.get_broker_config(state, broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')
        logger.warning(f"[{broker_name}] Cannot subscribe to serial commands - public key not available")
        return

    topic = f"meshcore/{state.global_iata}/{state.repeater_pub_key}/serial/commands"

    broker = topics.get_broker_config(state, broker_idx)
    broker_name = broker.get('name', f'broker-{broker_idx}')
    try:
        client.subscribe(topic, qos=1)
        logger.info(f"[{broker_name}] Subscribed to remote serial: {topic}")
    except Exception as e:
        logger.error(f"[{broker_name}] Error subscribing to {topic}: {e}")


def handle_serial_command(state: BridgeState, jwt_token: str, broker_idx: int) -> None:
    """Process an incoming serial command JWT."""
    if not state.remote_serial_enabled:
        logger.warning("[SERIAL] Remote serial command received but feature is disabled")
        return

    if not state.remote_serial_allowed_companions:
        logger.warning("[SERIAL] Remote serial command received but no companions are allowed")
        return

    if not state.auth:
        logger.error("[SERIAL] Auth provider not available")
        return

    # First decode without verification to get the public key
    try:
        payload = state.auth.decode_payload(jwt_token)
    except Exception as e:
        logger.warning(f"[SERIAL] Failed to decode command JWT: {e}")
        return

    # Extract and validate required fields
    companion_pubkey = payload.get('publicKey', '').upper()
    command = payload.get('command', '')
    target = payload.get('target', '').upper()
    nonce = payload.get('nonce', '')
    exp = payload.get('exp')

    if not companion_pubkey or not command or not target or not nonce:
        logger.warning("[SERIAL] Missing required fields in command JWT")
        return

    # Verify target matches our public key
    if target != state.repeater_pub_key:
        logger.debug(f"[SERIAL] Command target {target[:8]}... doesn't match our key {state.repeater_pub_key[:8]}...")
        return

    # Verify companion is in allowlist
    if companion_pubkey not in state.remote_serial_allowed_companions:
        logger.warning(f"[SERIAL] Command from unauthorized companion: {companion_pubkey[:16]}...")
        publish_serial_response(state, command, nonce, False, "Unauthorized companion", broker_idx)
        return

    # Check expiry against our system clock
    current_time = int(time.time())
    if exp and current_time > exp:
        logger.warning(f"[SERIAL] Command JWT expired (exp={exp}, now={current_time})")
        publish_serial_response(state, command, nonce, False, "Command expired", broker_idx)
        return

    # Check nonce for replay protection
    cleanup_old_nonces(state)
    if nonce in state.remote_serial_nonces:
        logger.warning(f"[SERIAL] Duplicate nonce detected (replay attack?): {nonce[:16]}...")
        return  # Silently drop replays

    # Verify JWT signature
    try:
        state.auth.verify_token(jwt_token, companion_pubkey)
        logger.debug(f"[SERIAL] JWT signature verified for companion {companion_pubkey[:16]}...")
    except Exception as e:
        logger.warning(f"[SERIAL] JWT signature verification failed: {e}")
        publish_serial_response(state, command, nonce, False, "Invalid signature", broker_idx)
        return

    # Record nonce to prevent replay
    state.remote_serial_nonces[nonce] = current_time

    # Check if command is disallowed
    allowed, matched_rule = is_command_allowed(state, command)
    if not allowed:
        logger.warning(f"[SERIAL] Command blocked by rule '{matched_rule}': {command}")
        publish_serial_response(state, command, nonce, False, f"Command blocked: {matched_rule}", broker_idx)
        return

    # Execute the serial command
    if not state.device:
        publish_serial_response(state, command, nonce, False, "Serial port not connected", broker_idx)
        return

    logger.info(f"[SERIAL] Executing command from {companion_pubkey[:16]}...: {command}")
    success, response = state.device.execute_command(command, timeout=state.remote_serial_command_timeout)

    # Publish response
    publish_serial_response(state, command, nonce, success, response, broker_idx)


def publish_serial_response(
    state: BridgeState,
    command: str,
    request_id: str,
    success: bool,
    response: str,
    broker_idx: int | None = None,
) -> None:
    """Create and publish a signed response JWT."""
    if not state.repeater_priv_key or not state.repeater_pub_key:
        logger.error("[SERIAL] Cannot sign response - private key not available")
        return

    if not state.auth:
        logger.error("[SERIAL] Auth provider not available")
        return

    try:
        claims = {
            'command': command,
            'request_id': request_id,
            'success': success,
            'response': response
        }

        response_jwt = state.auth.create_token(
            state.repeater_pub_key,
            state.repeater_priv_key,
            expiry_seconds=60,
            **claims
        )

        response_topic = f"meshcore/{state.global_iata}/{state.repeater_pub_key}/serial/responses"

        published = False
        for mqtt_info in state.mqtt_clients:
            if mqtt_info.get('connected', False):
                try:
                    broker = topics.get_broker_config(state, mqtt_info['broker_idx'])
                    broker_name = broker.get('name', f"broker-{mqtt_info['broker_idx']}")
                    result = mqtt_info['client'].publish(response_topic, response_jwt, qos=1)
                    if result:
                        published = True
                        logger.debug(f"[{broker_name}] Published serial response to {response_topic}")
                except Exception as e:
                    broker = topics.get_broker_config(state, mqtt_info['broker_idx'])
                    broker_name = broker.get('name', f"broker-{mqtt_info['broker_idx']}")
                    logger.error(f"[{broker_name}] Failed to publish serial response: {e}")

        if published:
            logger.info(f"[SERIAL] Response published (success={success}, request_id={request_id[:16]}...)")
        else:
            logger.error("[SERIAL] Failed to publish response to any broker")

    except Exception as e:
        logger.error(f"[SERIAL] Failed to create/publish response: {e}")
