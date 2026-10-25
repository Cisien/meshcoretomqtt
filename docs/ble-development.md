# BLE observer development

User setup is in the [README](../README.md#optional-ble-repeater-observer-experimental).
These notes explain the implementation and how to test changes. Contributor
conventions are in [AGENTS.md](../AGENTS.md).

## Data path

`bridge/ble_connection.py` implements the existing device-transport interface.
It assembles framed BLE notifications into complete log records, then feeds the
same packet parser and MQTT publishing path used by USB serial.

- A record is acknowledged only after complete, ordered reassembly in host RAM.
- An acknowledgement does not mean MQTT delivery. A crash between ACK and
  publication can lose data; there is no durable queue or replay guarantee.
- BLE delivery failures and MQTT publication failures have separate counters.

## Connection lifecycle

Bleak operations run on a dedicated event-loop thread. Callbacks carry a
connection generation so callbacks from an old link cannot affect a new one.
Teardown is bounded; it does not reset the Bluetooth adapter or change pairing.

Backend cleanup must run even after the physical link drops. In Bleak 3.0.2,
the remote-disconnect callback leaves its D-Bus connection open until public
`disconnect()` runs. Leaving that owner alive can preserve a BlueZ notification
subscription across reconnects and produce duplicate notifications. Only
`stop_notify()` requires a connected link. A hanging backend can still exceed
the cleanup budget; the fix is not a guarantee of cleanup under every failure.

Runner cleanup also covers failed startup getters, initialization exceptions
and exhausted initial MQTT retries. A stop request between retries must not
start another attempt.

## Tests

Run the full suite from the repository root:

```sh
uv run --system-certs --frozen --extra test pytest
```

For an offline run, prefix the command with `MCTOMQTT_SKIP_NETWORK=1`.
This skips external-service checks; do not describe it as passing network tests.

Tests inject device/broker I/O fakes while running real transport, parser and
lifecycle code. `tests/test_ble_disconnected_cleanup.py` checks resource release
after link loss; its optional real-Bleak case skips when Bleak is unavailable.
The normal PR CI does not install Bleak or exercise Bluetooth hardware.

Cross-layer wire checks need `OBSERVER_SOURCE` pointing to the exact patched
firmware checkout. Without it, a standalone bridge checkout skips those checks;
an explicitly invalid path fails. Supply that source when running these checks
separately; the normal PR CI does not provide it.
These source checks and unit tests do not establish RF, power, security or
long-duration reliability on hardware; record those results separately.
