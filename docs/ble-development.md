# BLE observer development

These notes are for contributors. User requirements and configuration are in
the [README](../README.md#optional-ble-repeater-observer-experimental).
See [AGENTS.md](../AGENTS.md) for contributor rules and test boundaries.

## Backend resource cleanup

BLE teardown releases backend resources even when the physical link has already
dropped. With Bleak 3.0.2, remote disconnection alone leaves its D-Bus connection
open, allowing stale BlueZ notification sessions to survive reconnects. Cleanup
still uses the existing bounded timeouts; frame validation, ACK rules, pairing
and reconnect policy are unchanged. Offline regressions cover resource release,
including the real Bleak backend when installed, but do not certify long-duration
hardware reliability or guarantee cleanup if the backend hangs.

## Record delivery

Complete records are ACKed after bounded host RAM reassembly, **not** MQTT
receipt. BLE and MQTT loss/failure counters remain separate; there is no durable
MQTT replay guarantee. Full power/RF, security/interruption and unattended field
acceptance remain outside the short initial hardware checks.

## Startup cleanup and test conventions

Device cleanup encloses initialization as well as packet processing. Failed
startup getters, initialization exceptions and exhausted initial MQTT retries
release the device; stop requests between retries do not start another attempt.
This is not durable offline delivery or a guarantee against every forced-kill,
controller or peripheral failure. No automatic Bluetooth reset is performed.

External I/O is supplied through explicit factories for embedding and tests;
normal CLI defaults, serial selection and network timeouts are unchanged.
BLE tests use injected fakes, real temporary files and real UTC timestamp
validation rather than global clock/core-module mocks. Permission fixtures use
an explicit ownership callable with real filesystem operations. Installer
runtime dependency argv is tested without executing installs or suppressing file
operations.

## Cross-layer validation

Standalone bridge tests report firmware-dependent C++ wire checks as skipped
when no firmware tree is present. For cross-layer validation set
`OBSERVER_SOURCE` to the exact reviewed, patched firmware checkout; an explicit
invalid source fails rather than skipping. The whole-stack candidate workflow
supplies that source and runs these checks.
