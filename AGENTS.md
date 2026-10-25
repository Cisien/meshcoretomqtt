# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

meshcoretomqtt is a Python bridge that reads serial data from MeshCore LoRa repeaters and publishes parsed packets/debug data to one or more MQTT brokers. It runs as a long-lived daemon, typically deployed as a systemd/launchd service, Docker container, or NixOS module.

**Environment note:** This software often runs on low-spec hardware (e.g. Raspberry Pi Zero) and at remote sites with low-bandwidth or high-latency connections. Network operations should use generous timeouts where appropriate.

## Running and Building

**Run locally (no build step):**
```bash
python3 mctomqtt.py --config config.toml.example          # requires pyserial and paho-mqtt
python3 mctomqtt.py --config config.toml.example --debug    # enables DEBUG-level logging
```

**Python dependencies:** `pyserial`, `paho-mqtt`, `ed25519-orlp`, and `bleak>=0.22` (install via pip or venv). Requires Python 3.11+ for `tomllib` stdlib. BLE use also requires a functioning BlueZ system service on Linux. Note: `ed25519-orlp` may require a C toolchain and Python headers on Linux if a pre-compiled wheel is unavailable.

**Docker:**
```bash
docker build -t mctomqtt:latest .
docker run -d --name mctomqtt --device=/dev/ttyACM0 \
  -v /path/to/mctomqtt-config:/etc/mctomqtt:ro \
  mctomqtt:latest
```

**NixOS:** `nix build` produces the default package. The flake also exports a NixOS module at `nixosModules.default`.

**Tests:** `python3 -m pytest tests/` (requires `pytest>=7.0`, declared in `pyproject.toml[project.optional-dependencies.test]`). GitHub Actions runs this on pull requests as a non-e2e test job. See the **Testing** section below for details.

## Architecture

The runtime codebase is a `bridge/` Python package with a thin entry point (project metadata in `pyproject.toml`):

- **`mctomqtt.py`** — Thin entry point (~45 lines). Keeps `__version__`, argparse, logging setup. Creates `MeshCoreBridge(config, debug, version)` and calls `bridge.run()`.

- **`bridge/`** — Python package containing all application logic, split into focused modules:
  - **`serial_connection.py`** — `SerialConnection` ABC + `RealSerialConnection` (device I/O with internal locking) + `connect()` factory
  - **`ble_connection.py`** — Bonded observer transport: 16-byte `<QIHH` framing, dedicated complete-record ACK/stats characteristics, bounded process-memory dedup across reconnects; GATT writes serialize independently of command-reply waits. Firmware counters live in `observer_firmware`, host counters in `observer_host`; MQTT failures remain independent. `runner.process_device_line()` clears pending RAW only at transport discontinuities, leaving the classic parser unchanged. Unit tests do not certify hardware; other boards and host platforms remain unvalidated.
  - **`auth_provider.py`** — `AuthProvider` ABC + `MeshCoreAuthProvider` (wraps `auth_token.py`)
  - **`broker_client.py`** — `BrokerClient` ABC + `PahoBrokerClient` (wraps paho-mqtt)
  - **`state.py`** — `BridgeState` shared mutable state container (all ~30 instance variables)
  - **`topics.py`** — Topic resolution: `get_topic()`, `resolve_topic_template()`, `sanitize_client_id()`
  - **`mqtt_publish.py`** — `safe_publish()`, `build_status_message()`, `publish_status()`
  - **`message_parser.py`** — `RAW_PATTERN`, `PACKET_PATTERN`, `parse_and_publish()`
  - **`remote_serial.py`** — Remote serial command handling, nonce management, JWT validation
  - **`background.py`** — `stats_logging_loop()`, `websocket_ping_loop()`
  - **`mqtt_manager.py`** — `MqttManager` class orchestrating broker connections, reconnection, callbacks
  - **`runner.py`** — `run()` main loop, `handle_signal()`, `wait_for_system_time_sync()`
  - **`__init__.py`** — `MeshCoreBridge` facade class

- **`auth_token.py`** — Provides JWT operations (`create_auth_token`, `verify_auth_token`, `decode_token_payload`) using `ed25519-orlp`.

- **`config_loader.py`** — TOML config loading with layered override system.

### Testability

Three ABC boundaries (`SerialConnection`, `AuthProvider`, `BrokerClient`) allow full control over external dependencies in tests. Fakes are in `tests/fakes.py`:
- `FakeSerialConnection` — returns canned device values
- `FakeAuthProvider` — returns deterministic tokens, configurable verify/reject
- `FakeBrokerClient` — records published messages for assertion
- `make_test_state()` — factory for `BridgeState` with fake injection

## Configuration System

Configuration uses TOML files with a layered override system. Python 3.11+ `tomllib` is used (stdlib, no third-party dependency).

**Default config loading** (no `--config` flags):
1. `/etc/mctomqtt/config.toml` (base defaults, overwritten on updates)
2. `/etc/mctomqtt/config.d/10-*.toml` (broker presets selected during install)
3. `/etc/mctomqtt/config.d/99-user.toml` (local user overrides, loaded last)

**`--config` override:** When one or more `--config <path>` flags are provided, default config loading is completely bypassed. Only the specified files are loaded, in order, each overlaying the previous. Multiple `--config` flags are supported for layered overrides.

**Override mechanism:** Drop-in files are deep-merged over the base config. Nested dicts are merged recursively; `[[broker]]` arrays are merged by `name` field.

**Key config sections:** `[general]`, `[serial]`, `[topics]`, `[remote_serial]`, `[update]`, `[[broker]]` with nested `[broker.tls]` and `[broker.auth]`.

**Broker auth methods:** `"password"` (username/password), `"token"` (JWT from device Ed25519 key), or `"none"`.

**Broker presets:** Community broker presets live in repo `presets/` as TOML files with `[[broker]]` blocks. The installer copies selected or imported presets to `/etc/mctomqtt/config.d/10-<preset>.toml`.

See `config.toml.example` for the full reference with all options and defaults.

## Directory Layout (System Install)

```
/opt/mctomqtt/              # App home (owned by mctomqtt:mctomqtt)
  mctomqtt.py
  auth_token.py
  config_loader.py
  bridge/                   # Application package
  .version_info
  venv/                     # Python venv (pyserial, paho-mqtt, ed25519-orlp)

/etc/mctomqtt/              # Config (owned root:mctomqtt, 750)
  config.toml               # Defaults (640, overwritten on updates)
  config.d/
    10-*.toml               # Broker presets
    99-user.toml            # User config (640, never overwritten)
```

## Key Patterns

- **Thread safety:** Serial port access is protected by internal locking in `RealSerialConnection`. The main loop, stats thread, and remote serial handler all call methods on the `SerialConnection` ABC — the lock is never exposed to callers.
- **MQTT auth:** Two modes per broker — username/password or JWT auth tokens (generated from device's Ed25519 private key). Tokens are cached with TTL. Auth operations go through the `AuthProvider` ABC.
- **Graceful shutdown:** SIGTERM/SIGINT handlers set `state.should_exit = True`. The main loop checks this flag each iteration.
- **BLE unit restart:** `systemd/meshcoretomqtt-ble.service` uses `Restart=always` because unavailable startup currently returns exit 0; explicit supervisor stop suppresses restart and uses SIGTERM. Offline process/policy tests do not prove installed-unit enforcement; real BlueZ backend behavior requires a separately approved hardware session. Do not change shared USB runner semantics to satisfy the unit.
- **Reproducible dependencies:** Keep `uv.lock` tracked and consistent with `pyproject.toml` (`uv lock --check`); clean CI uses `uv sync --system-certs --frozen --extra test`, never an untracked local lock/venv.
- **BLE session retirement:** Keep the active nonce/high-water mark across reconnects. Retired sessions are bounded (63 plus active) and age out only at a fresh validated connection boundary, with old callbacks generation-fenced. Exhaustion within a live generation requests the normal runner reconnect, not a process restart. Firmware sequence 1 after exhaustion requires a fresh nonce; `observer_firmware.unavailable_records/bytes` counts rejected admission while waiting for entropy, separately from host/MQTT loss.
- **Config access:** `state.config` dict with `state.config.get('section', {}).get('key', default)`. Broker configs accessed via `topics.get_broker_config(state, broker_idx)`.
- **Device transport selection:** Use `config_loader.resolve_transport()` everywhere (startup/reconnect, watchdog, summary). Canonical `[general].transport` wins; legacy top-level `transport` applies only if canonical is absent from merged config; neither means serial. Validate both supplied keys as exact `serial`/`ble` strings, including shadowed legacy values; no invalid-value/device-failure fallback. Broker TCP/WebSocket transport is a separate setting.
- **Bleak BlueZ security properties:** With the public BleakClient wrapper, Device1 properties live on `client._backend._device_info` (observed with Bleak 3.0.2), not `client._device_info`. Direct backend factories remain supported. Require literal `Paired is True` and `Trusted is True`; malformed/missing backend state must not fall back to wrapper attributes. This uses a version-sensitive private backend field; validate the installed Bleak/BlueZ versions in a separately approved hardware session with real BlueZ backend behavior. CI does not install Bleak and uses fakes only; Bleak upgrades may require re-validation of this backend property path.
- **BLE partial-record retries:** A valid offset-zero frame with the same nonce, sequence and total length replaces all partial bytes, without counting a whole-record loss. ACK/dedup happen only after complete ordered reassembly. Do not splice attempts or allow nonzero reordering/duplicates or a same-record length change.
- **BLE record ceiling:** Host `MAX_RECORD_BYTES` is 544 bytes including terminator, matching the observer firmware's `kMaximumRecordBytes`, `RecordQueue::kMaxRecordLength` and transport `LineBuffer` capacity. Change these together for a coordinated firmware/host baseline; verify the exact source and provenance of a matching image, never infer compatibility from the constant alone.
- **BLE teardown state:** `is_open` requires a running owner loop as well as the backend connection flag. Bounded teardown can leave cached `is_connected=true`; report the transport unusable but preserve the raw flag and refuse a second link until the backend reports disconnection. Do not fabricate physical disconnect or extend command timeouts to hide this state.
  Run public backend `disconnect()` cleanup on the owner loop even after physical
  link loss; Bleak 3.0.2 otherwise retains its D-Bus owner and BlueZ can restore
  stale notification sessions on reconnect. Only `stop_notify` needs a live link.
  Preserve bounded cleanup and initially-connected-only disconnect accounting.
- **Runner startup cleanup:** `run()` owns one cleanup scope around startup and the main loop, including failed device getters, initialization exceptions and exhausted initial MQTT retries. The stats thread may not have started when cleanup runs. Do not restore an early-return path that bypasses device closure; retain real-runner/fake-GATT regression coverage. Stop requests between initial MQTT attempts must not schedule another attempt.
- **Version:** `__version__` is defined at the top of `mctomqtt.py`. The `.version_info` JSON file (created by installer) appends git hash info. Version is passed to `MeshCoreBridge(config, debug, version)`.
- **Dependency injection:** All external dependencies (serial, MQTT, auth) are abstracted behind ABCs. Tests inject fakes via `make_test_state()` from `tests/fakes.py`.
- **I/O construction:** `MeshCoreBridge` accepts optional `transport_factory` and `broker_client_factory`; `MqttManager` accepts a `client_factory`. `runner.run` accepts a transport factory and startup-wait callable. Defaults use the real transports, Paho client and original retry timings. Tests pass fakes explicitly rather than patching modules or freezing the global clock; no runtime configuration selects arbitrary Python callables.
- **Installer file operations:** Since the installer runs as root, use Python stdlib directly — `os.makedirs()`, `shutil.copy2()`, `os.chmod()`, `shutil.chown()`, `Path.write_text()`, `shutil.rmtree()`, `Path.unlink()`. Never shell out for file operations. Reserve `run_cmd()` for external tools with no Python equivalent (systemctl, docker, useradd, pip, etc.). All subprocess commands use list form (never `shell=True`).
- **Config permissions:** `set_permissions()` repairs config/config.d to `0750` and base/preset/user TOML to `0640`, root:service-group; broker reconfiguration must not widen overrides back to world-readable. Preserve the inherited serial app ownership/executable behavior. BLE preparation separately requires root-owned, non-service-writable code/venv; permission-helper tests do not approve a complete BLE installation.
- **Observer preflight:** `preflight_config.py` validates one explicit BLE config before the separate preparation-only observer unit. Enabled broker auth must use the runtime-supported `none`/`password`/`token` methods; password credentials and token audience are required when selected, and TLS option types must be booleans. It does not prove TLS policy, broker reachability, production mapping, or receipt. The active `meshcoretomqtt-ble.service` has no pre-start validator and uses default shared config; do not imply the separate preflight unit guards it.

## Development Guidelines

- **No mocks in tests** unless explicitly directed. Prefer extracting testable functions and testing them with real files (e.g., `tmp_path`). Mocks hide bugs and make tests brittle.
- **Every code change must include a pass on documentation and tests.** Update AGENTS.md, README.md, config.toml.example, and other relevant docs when behavior changes. Add or update tests to cover the change.

## Testing

**Run:** `python3 -m pytest tests/` (or `pytest tests/`). Config is in `pyproject.toml`.

**Test tiers** (via pytest markers):
- **Default (no marker):** Pure-logic unit tests — validation, TOML generation, env parsing, config files, context. Always run, no dependencies.
- **`@pytest.mark.network`:** Tests needing internet (IATA API, download, bootstrap `--help`). Run by default; skip with `MCTOMQTT_SKIP_NETWORK=1`.
- **`@pytest.mark.system`:** Tests needing root + Linux (permissions, service user creation, systemd). Auto-skipped when not root; also skip with `MCTOMQTT_SKIP_SYSTEM=1`.
- **`@pytest.mark.e2e`:** Tests needing real services/devices. Opt-in only: `MCTOMQTT_TEST_E2E=1`.

**PR CI:** `.github/workflows/pr-tests.yaml` runs on `pull_request` and executes `python -m pytest tests/ -m "not e2e"` on Ubuntu. This includes the default unit tests plus any network/system tests that are functional in the GitHub runner, while still excluding opt-in e2e coverage that needs real services or devices.

**Conventions:**
- Cross-layer wire checks accept `OBSERVER_SOURCE` pointing to the exact patched
  firmware source. An explicitly invalid source fails; a standalone bridge
  checkout without firmware headers reports those checks as skipped. Whole-stack
  candidate CI supplies the validated source so the checks remain mandatory there.
- Test files mirror the module they test (e.g., `test_validation.py` tests `installer/config.py` validation helpers).
- Installer flow tests (`test_install_flow.py`, `test_update_flow.py`, `test_migrate_flow.py`) use `unittest.mock.patch` to stub interactive prompts and subprocess calls.
- Shell script syntax is validated via `bash -n` in `test_bash_bootstrap.py`.
- `test_ble_packaging.py` uses temporary roots and maps deployment ownership names to the unprivileged caller at the OS boundary while executing real chown/chmod/stat. It checks exact root/unit-service identity requests, not real inter-account access. `tests/helpers/offline_service.py` is test-only: real entrypoint/runner and SIGTERM in a fresh unprivileged subprocess, fake device/broker I/O, socket/device/subprocess audit guard. Do not invoke installers, systemctl, create system accounts or contact Bluetooth/MQTT for these offline tests.

## Deployment

### Installer Architecture

The installer is a Python package (`installer/`) with thin bash bootstraps. Python 3.11+ stdlib only (no pip dependencies for the installer itself).

**Privilege model:** The installer runs as root. Bash bootstraps auto-escalate via `exec sudo bash "$0" "$@"` if not already root. The Python entry point calls `require_root()` before dispatching any command. All file operations use Python stdlib (`os.makedirs`, `shutil.copy2`, `os.chmod`, `shutil.chown`, `Path.write_text`) directly — no `sudo` wrappers or temp-file-then-sudo-cp patterns. The only remaining `sudo` usage is `sudo -u <svc_user>` for privilege-dropping (running commands as the service user).

**Bash bootstraps** (~60 lines each, download the Python package and dispatch):
- **`install.sh`** — Runs `python3 -m installer install` (fresh install or update detection)
- **`scripts/update.sh`** — Runs `python3 -m installer update` (standalone update, reads repo/branch from existing config)
- **`scripts/migrate.sh`** — Runs `python3 -m installer migrate` (standalone migration from `~/.meshcoretomqtt`)

**Python installer modules** (`installer/`):
- **`__init__.py`** — `InstallerContext` dataclass (shared state: repo, branch, install_dir, config_dir, svc_user, etc.)
- **`__main__.py`** — argparse entry point with `install`, `update`, `migrate` subcommands; calls `require_root()` after arg parsing
- **`ui.py`** — ANSI color output (auto-detects TTY), prompts via `/dev/tty` (works with `curl | bash`)
- **`system.py`** — `run_cmd()` subprocess wrapper (for external tools only: systemctl, docker, useradd, etc.), `require_root()`, `chown_recursive()`, user management, service management (systemd/launchd/Docker), serial device detection, venv setup. Note: Set `INSTALL_REBUILD_VENV=1` to force a full virtual environment rebuild.
- **`config.py`** — Validation (email, pubkey, IATA), TOML generation, IATA API search via `urllib.request` + `json` (no jq dependency), interactive MQTT broker configuration flows
- **`install_cmd.py`** — Fresh install orchestration (delegates to migrate/update when appropriate)
- **`update_cmd.py`** — Update existing installation (file download, dependency refresh, config preservation, service restart)
- **`migrate_cmd.py`** — Legacy `.env`/`.env.local` to TOML conversion, old service cleanup

### Other Deployment Files

- **`pyproject.toml`** — Project metadata, Python version requirement (>=3.11), and pytest configuration.
- **`uninstall.sh`** — Interactive uninstaller that detects the service user from the systemd unit, stops/removes the service, offers config backup, and cleans up `/opt/mctomqtt/` and `/etc/mctomqtt/`.
- **`Dockerfile`** — Alpine build that includes all Python dependencies. Config mounted at `/etc/mctomqtt/config.toml`.
- **`mctomqtt.service`** — systemd unit template with security hardening (NoNewPrivileges, ProtectSystem, PrivateTmp).
- **`com.meshcore.mctomqtt.plist`** — macOS launchd plist for system-level daemon at `/Library/LaunchDaemons/`.
- **`configs/`** — User-contributed configuration examples.
- **`nix/`** — Nix flake with package definition (`packages.nix`), NixOS module (`nixos-module.nix`) that generates TOML config via `pkgs.formats.toml`, dev shell, and NixOS integration test.
