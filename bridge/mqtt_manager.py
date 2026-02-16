"""MQTT connection manager — orchestrates BrokerClient instances."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any, TYPE_CHECKING

from . import topics
from . import remote_serial
from . import background
from .broker_client import BrokerClient, PahoBrokerClient
from .mqtt_publish import build_status_message

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)


class MqttManager:
    """Orchestrates multiple MQTT broker connections."""

    def __init__(self, state: BridgeState) -> None:
        self.state = state

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect_all_brokers(self) -> bool:
        """Initial connection to all configured MQTT brokers."""
        state = self.state
        brokers = state.config.get('broker', [])

        logger.debug("=== MQTT Broker Configuration ===")
        for i, broker in enumerate(brokers):
            name = broker.get('name', f'broker-{i}')
            enabled = broker.get('enabled', False)
            if enabled:
                server = broker.get('server', 'unknown')
                port = broker.get('port', 1883)
                transport = broker.get('transport', 'tcp')
                tls_cfg = broker.get('tls', {})
                use_tls = tls_cfg.get('enabled', False)
                auth = broker.get('auth', {})
                auth_method = auth.get('method', 'none')
                logger.debug(f"  [{name}] ENABLED - {server}:{port} (transport={transport}, tls={use_tls}, auth={auth_method})")
            else:
                logger.debug(f"  [{name}] DISABLED")
        logger.debug("=================================")

        for i, broker in enumerate(brokers):
            state.connection_events[i] = threading.Event()

            client_info = self._create_and_connect_broker(i)
            if client_info:
                state.mqtt_clients.append(client_info)

        if len(state.mqtt_clients) == 0:
            logger.error("[MQTT] Failed to connect to any broker")
            return False

        logger.info(f"[MQTT] Initiated connection to {len(state.mqtt_clients)} broker(s)")

        # Wait for all brokers to complete initial connection attempt
        max_wait = 10
        for mqtt_info in state.mqtt_clients:
            broker_idx = mqtt_info['broker_idx']
            event = state.connection_events.get(broker_idx)
            if event:
                event.wait(timeout=max_wait)

        if not state.mqtt_connected:
            logger.error("[MQTT] No brokers connected after initial connection attempts")
            return False

        return True

    def reconnect_disconnected_brokers(self) -> None:
        """Check for disconnected brokers and recreate them."""
        state = self.state
        current_time = time.time()

        for i, mqtt_info in enumerate(state.mqtt_clients):
            if mqtt_info.get('connected', False):
                continue

            connecting_since = mqtt_info.get('connecting_since', 0)
            if connecting_since > 0 and (current_time - connecting_since) < 10:
                continue

            if current_time < mqtt_info.get('reconnect_at', 0):
                continue

            broker_idx = mqtt_info['broker_idx']
            broker = topics.get_broker_config(state, broker_idx)
            broker_name = broker.get('name', f'broker-{broker_idx}')
            failed_attempts = mqtt_info.get('failed_attempts', 0)

            if failed_attempts >= state.max_reconnect_attempts:
                logger.critical(f"[{broker_name}] {state.max_reconnect_attempts} consecutive failures - exiting for service restart")
                state.should_exit = True
                return

            logger.info(f"[{broker_name}] Reconnecting (attempt #{failed_attempts + 1})")

            # Stop old client cleanly
            old_client = mqtt_info.get('client')
            if old_client:
                try:
                    self.stop_websocket_ping_thread(broker_idx)
                    old_client.loop_stop()
                    old_client.disconnect()
                except Exception as e:
                    logger.debug(f"[{broker_name}] Error stopping old client: {e}")

            # Clear token cache to force fresh token
            if broker_idx in state.token_cache:
                del state.token_cache[broker_idx]

            # Create fresh client
            new_client_info = self._create_and_connect_broker(broker_idx)

            if new_client_info:
                state.mqtt_clients[i] = new_client_info
                logger.debug(f"[{broker_name}] Recreated client successfully")
            else:
                mqtt_info['failed_attempts'] = failed_attempts + 1
                jitter = random.uniform(-0.5, 0.5)
                delay = max(0, state.reconnect_delay + jitter)
                mqtt_info['reconnect_at'] = current_time + delay
                state.reconnect_delay = min(state.reconnect_delay * state.reconnect_backoff, state.max_reconnect_delay)
                logger.warning(f"[{broker_name}] Failed to recreate client (attempt #{failed_attempts + 1}/{state.max_reconnect_attempts})")

    def stop_websocket_ping_thread(self, broker_idx: int) -> None:
        """Cleanly stop the WebSocket ping thread for a broker."""
        state = self.state
        if broker_idx in state.ws_ping_threads:
            state.ws_ping_threads[broker_idx]['active'] = False
            time.sleep(0.1)
            del state.ws_ping_threads[broker_idx]
            broker = topics.get_broker_config(state, broker_idx)
            logger.debug(f"[{broker.get('name', broker_idx)}] Stopped WebSocket ping thread")

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def on_mqtt_connect(self, client: Any, userdata: dict[str, Any] | None, flags: Any, rc: int, properties: Any = None) -> None:
        state = self.state
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        broker_idx = userdata.get('broker_idx', None) if userdata else None

        if broker_idx in state.connection_events:
            state.connection_events[broker_idx].set()

        if rc == 0:
            state.reconnect_delay = 1.0

            mqtt_info = None
            for info in state.mqtt_clients:
                if info['broker_idx'] == broker_idx:
                    mqtt_info = info
                    break

            if not mqtt_info:
                logger.error(f"[{broker_name}] on_connect fired but broker not in mqtt_clients list")
                return

            current_time = time.time()
            was_connected = mqtt_info.get('connected', False)
            is_first_connect = mqtt_info.get('connect_time', 0) == 0

            mqtt_info['connected'] = True
            mqtt_info['connecting_since'] = 0
            mqtt_info['connect_time'] = current_time

            if was_connected and not is_first_connect:
                logger.info(f"[{broker_name}] Reconnected to broker")
            elif is_first_connect:
                logger.info(f"[{broker_name}] Connected to broker")
            else:
                logger.debug(f"[{broker_name}] Connection state updated")

            if not state.mqtt_connected:
                state.mqtt_connected = True

            # Publish online status
            status_topic = topics.get_topic(state, "status", broker_idx)
            status_payload = json.dumps(build_status_message(state, "online"))
            broker = topics.get_broker_config(state, broker_idx)
            qos = broker.get('qos', 0)
            retain = broker.get('retain', True)

            try:
                broker_client = mqtt_info['client']
                broker_client.publish(status_topic, status_payload, qos=qos, retain=retain)
            except Exception as e:
                logger.error(f"[{broker_name}] Failed to publish online status: {e}")

            # Subscribe to remote serial commands
            broker_client = mqtt_info['client']
            remote_serial.subscribe_serial_commands(state, broker_client, broker_idx)
        else:
            logger.error(f"[{broker_name}] Connection failed with code: {rc}")

    def on_mqtt_disconnect(self, client: Any, userdata: dict[str, Any] | None, disconnect_flags: Any, reason_code: Any, properties: Any) -> None:
        state = self.state
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        broker_idx = userdata.get('broker_idx', None) if userdata else None

        # During graceful shutdown, just mark disconnected and return
        if state.should_exit:
            for info in state.mqtt_clients:
                if info['broker_idx'] == broker_idx:
                    info['connected'] = False
                    break
            logger.debug(f"[{broker_name}] Disconnected (shutdown)")
            return

        if broker_idx in state.ws_ping_threads:
            state.ws_ping_threads[broker_idx]['active'] = False

        already_disconnected = False
        mqtt_info = None
        for info in state.mqtt_clients:
            if info['broker_idx'] == broker_idx:
                mqtt_info = info
                already_disconnected = not info.get('connected', False)
                info['connected'] = False
                info['connecting_since'] = 0
                info['reconnect_at'] = time.time() + state.reconnect_delay

                connect_time = info.get('connect_time', 0)
                if connect_time > 0 and (time.time() - connect_time) < 120:
                    info['failed_attempts'] = info.get('failed_attempts', 0) + 1
                    logger.warning(f"[{broker_name}] Short-lived connection detected (failed_attempts: {info['failed_attempts']})")
                elif connect_time > 0:
                    if info.get('failed_attempts', 0) > 0:
                        logger.info(f"[{broker_name}] Stable connection ended after {int(time.time() - connect_time)}s - resetting failure counter")
                        info['failed_attempts'] = 0

                break

        if not already_disconnected:
            logger.warning(f"[{broker_name}] Disconnected (code: {reason_code}, flags: {disconnect_flags}, properties: {properties})")

            if mqtt_info and mqtt_info.get('connect_time', 0) > 0:
                current_time = time.time()
                if 'reconnects' not in state.stats:
                    state.stats['reconnects'] = {}
                if broker_idx not in state.stats['reconnects']:
                    state.stats['reconnects'][broker_idx] = []
                state.stats['reconnects'][broker_idx].append(current_time)

        all_disconnected = all(not info.get('connected', False) for info in state.mqtt_clients)
        if all_disconnected:
            state.mqtt_connected = False

    def on_mqtt_message(self, client: Any, userdata: dict[str, Any] | None, msg: Any) -> None:
        """Handle incoming MQTT messages (for remote serial commands)."""
        state = self.state
        broker_idx = userdata.get('broker_idx', None) if userdata else None
        topic = msg.topic

        if '/serial/commands' not in topic:
            return

        broker = topics.get_broker_config(state, broker_idx) if broker_idx is not None else {}
        broker_name = broker.get('name', f'broker-{broker_idx}')
        logger.debug(f"[{broker_name}] Received message on {topic}")

        try:
            jwt_token = msg.payload.decode('utf-8').strip()
            remote_serial.handle_serial_command(state, jwt_token, broker_idx)
        except Exception as e:
            logger.error(f"[SERIAL] Failed to handle command: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_auth_credentials(self, broker_idx: int, force_refresh: bool = False) -> tuple[str | None, str | None]:
        """Generate authentication credentials for a broker on-demand."""
        state = self.state
        broker = topics.get_broker_config(state, broker_idx)
        auth = broker.get('auth', {})
        auth_method = auth.get('method', 'none')

        if auth_method == 'token':
            if not state.repeater_priv_key:
                logger.error(f"[{broker.get('name', broker_idx)}] Private key not available from device for auth token")
                return None, None

            current_time = time.time()
            if not force_refresh and broker_idx in state.token_cache:
                cached_token, created_at = state.token_cache[broker_idx]
                age = current_time - created_at
                if age < (state.token_ttl - 300):
                    logger.debug(f"[{broker.get('name', broker_idx)}] Using cached auth token (age: {age:.0f}s)")
                    username = f"v1_{state.repeater_pub_key.upper()}"
                    return username, cached_token

            try:
                username = f"v1_{state.repeater_pub_key.upper()}"
                audience = auth.get('audience', '')

                tls_cfg = broker.get('tls', {})
                use_tls = tls_cfg.get('enabled', False)
                tls_verify = tls_cfg.get('verify', True)
                secure_connection = use_tls and tls_verify

                owner = auth.get('owner', '')
                email = auth.get('email', '')

                claims: dict[str, Any] = {}
                if audience:
                    claims['aud'] = audience

                if secure_connection:
                    if owner:
                        claims['owner'] = owner
                    if email:
                        claims['email'] = email.lower()
                else:
                    if owner or email:
                        logger.debug(f"[{broker.get('name', broker_idx)}] Skipping email/owner in JWT - TLS and TLS verify must both be enabled")

                claims['client'] = state.client_version

                password = state.auth.create_token(state.repeater_pub_key, state.repeater_priv_key, expiry_seconds=state.token_ttl, **claims)
                state.token_cache[broker_idx] = (password, current_time)
                logger.debug(f"[{broker.get('name', broker_idx)}] Generated fresh auth token (1h expiry)")
                return username, password
            except Exception as e:
                logger.error(f"[{broker.get('name', broker_idx)}] Failed to generate auth token: {e}")
                return None, None
        elif auth_method == 'password':
            username = auth.get('username', '')
            password = auth.get('password', '')
            return username, password
        else:
            return '', ''

    def _create_broker_client(self, broker_idx: int) -> BrokerClient | None:
        """Create and configure a BrokerClient (doesn't connect)."""
        state = self.state
        broker = topics.get_broker_config(state, broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')

        # Build client ID
        prefix = "meshcore_"
        brokers = state.config.get('broker', [])
        if brokers:
            prefix = brokers[0].get('client_id_prefix', 'meshcore_')
        client_id = topics.sanitize_client_id(state.repeater_pub_key, prefix)
        if broker_idx > 0:
            client_id += f"_{broker_idx}"

        transport = broker.get('transport', 'tcp')

        # Get credentials
        username, password = self._generate_auth_credentials(broker_idx)
        if username is None:
            return None

        # Build LWT
        lwt_topic = topics.get_topic(state, "status", broker_idx)
        lwt_payload = json.dumps(build_status_message(state, "offline", include_stats=False))
        lwt_qos = broker.get('qos', 0)
        lwt_retain = broker.get('retain', True)

        # TLS config
        tls_cfg = broker.get('tls', {})
        tls_enabled = tls_cfg.get('enabled', False)
        tls_verify = tls_cfg.get('verify', True)
        if tls_enabled and not tls_verify:
            logger.warning(f"[{broker_name}] TLS verification disabled")

        broker_client = PahoBrokerClient(
            client_id=client_id,
            transport=transport,
            username=username if username else None,
            password=password,
            lwt_topic=lwt_topic,
            lwt_payload=lwt_payload,
            lwt_qos=lwt_qos,
            lwt_retain=lwt_retain,
            tls_enabled=tls_enabled,
            tls_verify=tls_verify,
            on_connect=self.on_mqtt_connect,
            on_disconnect=self.on_mqtt_disconnect,
            on_message=self.on_mqtt_message,
            userdata={'name': broker_name, 'broker_idx': broker_idx},
        )

        return broker_client

    def _create_and_connect_broker(self, broker_idx: int) -> dict[str, Any] | None:
        """Create a fresh broker client and connect it."""
        state = self.state
        if not state.repeater_name:
            logger.error("[MQTT] Cannot connect without repeater name")
            return None

        broker = topics.get_broker_config(state, broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')

        if not broker.get('enabled', False):
            logger.debug(f"[{broker_name}] Disabled, skipping")
            return None

        server = broker.get('server', '')
        if not server:
            logger.error(f"[{broker_name}] No server configured")
            return None

        port = broker.get('port', 1883)
        transport = broker.get('transport', 'tcp')
        keepalive = broker.get('keepalive', 60)
        tls_cfg = broker.get('tls', {})
        use_tls = tls_cfg.get('enabled', False)

        logger.debug(f"[{broker_name}] Creating fresh client")

        broker_client = self._create_broker_client(broker_idx)
        if not broker_client:
            return None

        try:
            broker_client.connect(server, port, keepalive=keepalive)
            broker_client.loop_start()

            if transport == "websockets":
                state.ws_ping_threads[broker_idx] = {'active': True}
                ping_thread = threading.Thread(
                    target=background.websocket_ping_loop,
                    args=(state, broker_idx, broker_client, transport),
                    daemon=True,
                    name=f"WS-Ping-{broker_name}"
                )
                ping_thread.start()

            logger.info(f"[{broker_name}] Connecting to {server}:{port} (transport={transport}, tls={use_tls}, keepalive={keepalive}s)")

            return {
                'client': broker_client,
                'broker_idx': broker_idx,
                'server': server,
                'port': port,
                'connected': False,
                'connecting_since': time.time(),
                'connect_time': 0,
                'reconnect_at': 0,
                'failed_attempts': 0
            }
        except Exception as e:
            logger.error(f"[{broker_name}] Failed to connect: {e}")
            return None
