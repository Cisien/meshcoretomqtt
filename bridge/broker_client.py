"""MQTT broker client abstraction."""
from __future__ import annotations

import logging
import ssl
from abc import ABC, abstractmethod
from typing import Any, Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class BrokerClient(ABC):
    """Abstract interface for a single MQTT broker connection."""

    @abstractmethod
    def connect(self, server: str, port: int, keepalive: int = 60) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool: ...

    @abstractmethod
    def subscribe(self, topic: str, qos: int = 0) -> None: ...

    @abstractmethod
    def loop_start(self) -> None: ...

    @abstractmethod
    def loop_stop(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...


class PahoBrokerClient(BrokerClient):
    """Concrete implementation wrapping paho.mqtt.client.Client."""

    def __init__(
        self,
        client_id: str,
        transport: str = 'tcp',
        username: str | None = None,
        password: str | None = None,
        lwt_topic: str | None = None,
        lwt_payload: str | None = None,
        lwt_qos: int = 0,
        lwt_retain: bool = True,
        tls_enabled: bool = False,
        tls_verify: bool = True,
        on_connect: Callable[..., Any] | None = None,
        on_disconnect: Callable[..., Any] | None = None,
        on_message: Callable[..., Any] | None = None,
        userdata: dict[str, Any] | None = None,
    ) -> None:
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            transport=transport
        )

        if userdata:
            self._client.user_data_set(userdata)

        if username:
            self._client.username_pw_set(username, password)

        if lwt_topic:
            self._client.will_set(lwt_topic, lwt_payload, qos=lwt_qos, retain=lwt_retain)

        if on_connect:
            self._client.on_connect = on_connect
        if on_disconnect:
            self._client.on_disconnect = on_disconnect
        if on_message:
            self._client.on_message = on_message

        if tls_enabled:
            if tls_verify:
                self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                self._client.tls_insecure_set(False)
            else:
                self._client.tls_set(cert_reqs=ssl.CERT_NONE)
                self._client.tls_insecure_set(True)

        if transport == "websockets":
            self._client.ws_set_options(path="/", headers=None)

        self._connected = False

    def connect(self, server: str, port: int, keepalive: int = 60) -> None:
        self._client.connect(server, port, keepalive=keepalive)

    def disconnect(self) -> None:
        self._client.disconnect()

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool:
        result = self._client.publish(topic, payload, qos=qos, retain=retain)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def subscribe(self, topic: str, qos: int = 0) -> None:
        result = self._client.subscribe(topic, qos=qos)
        if result[0] != mqtt.MQTT_ERR_SUCCESS:
            raise Exception(f"Subscribe failed: {mqtt.error_string(result[0])}")

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def raw_client(self) -> mqtt.Client:
        """Access the underlying paho client (for WebSocket ping etc.)."""
        return self._client
