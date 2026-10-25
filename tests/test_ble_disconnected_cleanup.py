"""Physical link loss must not bypass backend resource cleanup."""
import pytest

from test_ble_connection import (
    FakeGatt, close_ble_connections_after_test, make_connection,
)


class ResourceGatt(FakeGatt):
    def __init__(self):
        super().__init__()
        self.resources_open = True
        self.stop_attempts = 0

    async def stop_notify(self, uuid):
        self.stop_attempts += 1
        await super().stop_notify(uuid)

    async def disconnect(self):
        await super().disconnect()
        self.resources_open = False


def test_remote_link_loss_still_releases_backend_on_owner_loop():
    fake = ResourceGatt()
    connection = make_connection(fake)
    connection.open()
    loop, thread = connection._loop, connection._loop_thread.ident
    fake.is_connected = False

    connection.close()

    assert not fake.resources_open
    assert fake.stop_attempts == 0
    assert fake.operation_loops[-1] is loop
    assert fake.operation_threads[-1] == thread
    assert connection.metrics["disconnects"] == 0
    assert not connection.is_open


class PassiveBus:
    """External D-Bus transport boundary, never connected to a system bus."""
    def __init__(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    async def wait_for_disconnect(self):
        assert not self.connected


class BleakCleanupGatt(ResourceGatt):
    def __init__(self, backend):
        super().__init__()
        self._backend = backend
        backend._device_info = {"Paired": True, "Trusted": True}
        self.bus = PassiveBus()
        backend._bus = self.bus

    async def disconnect(self):
        self._record_operation()
        await self._backend.disconnect()
        self.resources_open = False
        self.is_connected = False


def test_real_bleak_disconnected_cleanup_closes_its_dbus_transport():
    # The normal no-Bleak CI still runs the backend-independent regression.
    # When available, exercise real BlueZ backend cleanup with only I/O faked.
    bleak = pytest.importorskip("bleak.backends.bluezdbus.client")
    backend = bleak.BleakClientBlueZDBus("02:00:00:00:00:01", bluez={}, timeout=10)
    fake = BleakCleanupGatt(backend)
    connection = make_connection(fake)
    connection.open()
    loop, thread = connection._loop, connection._loop_thread.ident
    fake.is_connected = False
    backend._is_connected = False
    backend._cleanup_all()

    connection.close()

    assert not fake.bus.connected
    assert backend._bus is None
    assert fake.stop_attempts == 0
    assert fake.operation_loops[-1] is loop
    assert fake.operation_threads[-1] == thread
    assert connection.metrics["disconnects"] == 0
    assert not connection.is_open
