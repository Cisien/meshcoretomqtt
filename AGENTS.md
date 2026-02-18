# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

meshcoretomqtt is a Python bridge that reads serial data from MeshCore LoRa repeaters and publishes parsed packets/debug data to one or more MQTT brokers. It runs as a long-lived daemon, typically deployed as a systemd/launchd service, Docker container, or NixOS module.

## Running and Building

**Run locally (no build step):**
```bash
python3 mctomqtt.py --config config.toml.example          # requires pyserial and paho-mqtt
python3 mctomqtt.py --config config.toml.example --debug    # enables DEBUG-level logging
```

**Python dependencies:** `pyserial`, `paho-mqtt` (install via pip or venv). Requires Python 3.11+ for `tomllib` stdlib.

**Optional dependency:** `meshcore-decoder` (Node.js CLI, `npm install -g @michaelhart/meshcore-decoder`) — required for JWT auth token generation/verification.

**Docker:**
```bash
docker build -t mctomqtt:latest .
docker run -d --name mctomqtt --device=/dev/ttyACM0 \
  -v /path/to/config.toml:/etc/mctomqtt/config.toml \
  mctomqtt:latest
```

**NixOS:** `nix build` produces the default package. The flake also exports a NixOS module at `nixosModules.default`.

**Tests:** `python3 -m pytest tests/` (requires `pytest>=7.0`, declared in `pyproject.toml[project.optional-dependencies.test]`). See the **Testing** section below for details.

## Architecture

The runtime codebase is a `bridge/` Python package with a thin entry point (project metadata in `pyproject.toml`):

- **`mctomqtt.py`** — Thin entry point (~45 lines). Keeps `__version__`, argparse, logging setup. Creates `MeshCoreBridge(config, debug, version)` and calls `bridge.run()`.

- **`bridge/`** — Python package containing all application logic, split into focused modules:
  - **`serial_connection.py`** — `SerialConnection` ABC + `RealSerialConnection` (device I/O with internal locking) + `connect()` factory
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

- **`auth_token.py`** — Thin wrapper around the `meshcore-decoder` CLI for JWT operations (`create_auth_token`, `verify_auth_token`, `decode_token_payload`). All crypto is delegated to the external Node.js tool via `subprocess.run`.

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
2. `/etc/mctomqtt/config.d/*.toml` (drop-in overrides, alphabetical order)

**`--config` override:** When one or more `--config <path>` flags are provided, default config loading is completely bypassed. Only the specified files are loaded, in order, each overlaying the previous. Multiple `--config` flags are supported for layered overrides.

**Override mechanism:** Drop-in files are deep-merged over the base config. Nested dicts are merged recursively; `[[broker]]` arrays are merged by `name` field.

**Key config sections:** `[general]`, `[serial]`, `[topics]`, `[remote_serial]`, `[update]`, `[[broker]]` with nested `[broker.tls]` and `[broker.auth]`.

**Broker auth methods:** `"password"` (username/password), `"token"` (JWT from device Ed25519 key), or `"none"`.

See `config.toml.example` for the full reference with all options and defaults.

## Directory Layout (System Install)

```
/opt/mctomqtt/              # App home (owned by mctomqtt:mctomqtt)
  mctomqtt.py
  auth_token.py
  config_loader.py
  bridge/                   # Application package
  .version_info
  venv/                     # Python venv (pyserial, paho-mqtt)
  .nvm/                     # NVM + Node LTS + meshcore-decoder

/etc/mctomqtt/              # Config (owned root:mctomqtt, 750)
  config.toml               # Defaults (640, overwritten on updates)
  config.d/
    00-user.toml            # User config (640, never overwritten)
```

## Key Patterns

- **Thread safety:** Serial port access is protected by internal locking in `RealSerialConnection`. The main loop, stats thread, and remote serial handler all call methods on the `SerialConnection` ABC — the lock is never exposed to callers.
- **MQTT auth:** Two modes per broker — username/password or JWT auth tokens (generated from device's Ed25519 private key via meshcore-decoder). Tokens are cached with TTL. Auth operations go through the `AuthProvider` ABC.
- **Graceful shutdown:** SIGTERM/SIGINT handlers set `state.should_exit = True`. The main loop checks this flag each iteration.
- **Config access:** `state.config` dict with `state.config.get('section', {}).get('key', default)`. Broker configs accessed via `topics.get_broker_config(state, broker_idx)`.
- **Version:** `__version__` is defined at the top of `mctomqtt.py`. The `.version_info` JSON file (created by installer) appends git hash info. Version is passed to `MeshCoreBridge(config, debug, version)`.
- **Dependency injection:** All external dependencies (serial, MQTT, auth) are abstracted behind ABCs. Tests inject fakes via `make_test_state()` from `tests/fakes.py`.
- **Installer file operations:** Since the installer runs as root, use Python stdlib directly — `os.makedirs()`, `shutil.copy2()`, `os.chmod()`, `shutil.chown()`, `Path.write_text()`, `shutil.rmtree()`, `Path.unlink()`. Never shell out for file operations. Reserve `run_cmd()` for external tools with no Python equivalent (systemctl, docker, useradd, pip, etc.). All subprocess commands use list form (never `shell=True`).

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

**Conventions:**
- Test files mirror the module they test (e.g., `test_validation.py` tests `installer/config.py` validation helpers).
- Installer flow tests (`test_install_flow.py`, `test_update_flow.py`, `test_migrate_flow.py`) use `unittest.mock.patch` to stub interactive prompts and subprocess calls.
- Shell script syntax is validated via `bash -n` in `test_bash_bootstrap.py`.

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
- **`system.py`** — `run_cmd()` subprocess wrapper (for external tools only: systemctl, docker, useradd, etc.), `require_root()`, `chown_recursive()`, user management, service management (systemd/launchd/Docker), serial device detection, venv/NVM setup, version info
- **`config.py`** — Validation (email, pubkey, IATA), TOML generation, IATA API search via `urllib.request` + `json` (no jq dependency), interactive MQTT broker configuration flows
- **`install_cmd.py`** — Fresh install orchestration (delegates to migrate/update when appropriate)
- **`update_cmd.py`** — Update existing installation (file download, dependency refresh, config preservation, service restart)
- **`migrate_cmd.py`** — Legacy `.env`/`.env.local` to TOML conversion, old service cleanup

### Other Deployment Files

- **`pyproject.toml`** — Project metadata, Python version requirement (>=3.11), and pytest configuration.
- **`uninstall.sh`** — Interactive uninstaller that detects the service user from the systemd unit, stops/removes the service, offers config backup, and cleans up `/opt/mctomqtt/` and `/etc/mctomqtt/`.
- **`Dockerfile`** — Multi-stage Alpine build that includes Node.js runtime and meshcore-decoder. Config mounted at `/etc/mctomqtt/config.toml`.
- **`mctomqtt.service`** — systemd unit template with security hardening (NoNewPrivileges, ProtectSystem, PrivateTmp).
- **`com.meshcore.mctomqtt.plist`** — macOS launchd plist for system-level daemon at `/Library/LaunchDaemons/`.
- **`configs/`** — User-contributed configuration examples.
- **`nix/`** — Nix flake with package definition (`packages.nix`), NixOS module (`nixos-module.nix`) that generates TOML config via `pkgs.formats.toml`, dev shell, and NixOS integration test.
