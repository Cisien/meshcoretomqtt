"""Tests for background stats refresh and MQTT status publishing."""
from __future__ import annotations

import json
import threading

from bridge import background
from tests.fakes import FakeBrokerClient, FakeSerialConnection, make_test_state


def test_stats_refresh_event_polls_and_publishes_without_periodic_sleep(monkeypatch):
    client = FakeBrokerClient()
    client.connect("localhost", 1883)
    broker_info = {"client": client, "broker_idx": 0, "connected": True}
    state = make_test_state(device=None, broker_clients=[broker_info])
    refresh_event = getattr(state, "device_stats_refresh_event", None)
    assert refresh_event is not None, "BridgeState must expose a stats refresh event"
    refresh_event.set()

    class ExitAfterStatsConnection(FakeSerialConnection):
        def __init__(self):
            super().__init__(stats={"battery_mv": 4100, "uptime_secs": 120})
            self.stats_calls = 0

        def get_device_stats(self):
            self.stats_calls += 1
            state.should_exit = True
            return super().get_device_stats()

    device = ExitAfterStatsConnection()
    state.device = device
    sleeps = []
    monkeypatch.setattr(background, "sleep", lambda seconds: sleeps.append(seconds))

    background.stats_logging_loop(state)

    assert sleeps == []
    assert device.stats_calls == 1
    status_messages = [
        json.loads(payload)
        for topic, payload, _qos, _retain in client.published
        if topic.endswith("/status")
    ]
    assert len(status_messages) == 1
    assert status_messages[0]["status"] == "online"
    assert status_messages[0]["stats"] == {"battery_mv": 4100, "uptime_secs": 120}


def test_stats_refresh_timeout_keeps_periodic_polling(monkeypatch):
    class TimeoutEvent:
        def __init__(self):
            self.wait_timeouts = []

        def wait(self, timeout):
            self.wait_timeouts.append(timeout)
            return False

        def clear(self):
            pass

    state = make_test_state(device=None)
    timeout_event = TimeoutEvent()
    state.device_stats_refresh_event = timeout_event

    class ExitAfterStatsConnection(FakeSerialConnection):
        def get_device_stats(self):
            state.should_exit = True
            return {"uptime_secs": 300}

    device = ExitAfterStatsConnection()
    state.device = device
    monkeypatch.setattr(background, "sleep", lambda _seconds: None)
    background.stats_logging_loop(state)

    assert timeout_event.wait_timeouts == [300]
    assert state.stats["device"] == {"uptime_secs": 300}
