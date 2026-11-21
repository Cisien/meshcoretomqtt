"""Clock restoration at CoC connection boundaries, without hardware I/O."""
import pytest
from types import SimpleNamespace

from bridge import runner
from bridge.coc_connection import CocConnection
from tests.fakes import FakeSerialConnection, make_test_state


@pytest.mark.parametrize("enabled,synchronized,expected", [
    (True, True, True), (True, False, False), (False, True, False),
])
def test_coc_connection_obeys_clock_policy(enabled, synchronized, expected):
    state = make_test_state(config={"general": {"transport": "coc", "sync_time": enabled}})
    state.sync_time_at_start = enabled
    device = FakeSerialConnection()
    result = runner._connect_device(state, lambda config: device,
                                    time_sync_wait=lambda state: synchronized)
    assert result is device
    assert state.device is device
    assert device.time_set is expected


def test_radio_reconnect_restores_clock_without_restarting_bridge():
    state = make_test_state(config={"general": {"transport": "coc"}})
    state.sync_time_at_start = True
    devices = [FakeSerialConnection(), FakeSerialConnection()]
    for device in devices:
        runner._connect_device(state, lambda config: device,
                               time_sync_wait=lambda state: True)
    assert all(device.time_set for device in devices)


@pytest.mark.parametrize("transport", ["serial"])
def test_other_transports_keep_existing_startup_policy(transport):
    state = make_test_state(config={"general": {"transport": transport}})
    state.sync_time_at_start = True
    device = FakeSerialConnection()
    runner._connect_device(state, lambda config: device,
                           time_sync_wait=lambda state: pytest.fail("unexpected sync"))
    assert not device.time_set


def test_unavailable_device_does_not_sync():
    state = make_test_state(config={"general": {"transport": "coc"}})
    assert runner._connect_device(
        state, lambda config: None,
        time_sync_wait=lambda state: pytest.fail("unavailable device"),
    ) is None


def test_shutdown_does_not_write_clock():
    state = make_test_state(config={"general": {"transport": "coc"}})
    state.should_exit = True
    device = FakeSerialConnection()
    runner._connect_device(state, lambda config: device,
                           time_sync_wait=lambda state: True)
    assert not device.time_set


@pytest.mark.parametrize("result", [
    SimpleNamespace(stdout="no\n", stderr="", returncode=0),
    FileNotFoundError(), OSError(),
])
def test_coc_never_uses_unverified_host_clock(result):
    state = make_test_state()
    def read_status():
        if isinstance(result, Exception):
            raise result
        return result
    assert runner.wait_for_system_time_sync(
        state, require_sync=True, status_reader=read_status, retry_wait=lambda: None,
    ) is False


def test_coc_accepts_verified_ntp_property_independent_of_display_locale():
    state = make_test_state()
    assert runner.wait_for_system_time_sync(
        state, require_sync=True,
        status_reader=lambda: SimpleNamespace(stdout="yes\n", stderr="", returncode=0),
        retry_wait=lambda: pytest.fail("already synchronized"),
    ) is True


@pytest.mark.parametrize("ok,response", [
    (False, "Command timeout"), (True, "ERR: rejected"), (True, "(no output)"),
    (True, "OK - clock set failed"), (True, "OK - clock setter"),
    (True, "(ERR: clock cannot go backwards)"),
])
def test_coc_clock_write_requires_positive_reply(ok, response):
    class Reply(CocConnection):
        def __init__(self):
            pass

        def execute_command(self, command, timeout=10.0):
            assert command.startswith("time ")
            return ok, response

    with pytest.raises(OSError, match="clock synchronization"):
        Reply().set_time()


@pytest.mark.parametrize("response", [
    "OK - clock set",
    "OK - clock set: 12:00 - 30/10/2026 UTC",
])
def test_coc_accepts_released_and_upstream_clock_success(response):
    class Reply(CocConnection):
        def __init__(self):
            pass

        def execute_command(self, command, timeout=10.0):
            assert command.startswith("time ")
            return True, response

    Reply().set_time()
