# meshcoretomqtt

A Python-based script to send MeshCore debug and packet capture data to MQTT for
analysis. Requires a MeshCore repeater to be connected to a Raspberry Pi,
server, or similar device running Python.

The goal is to have multiple repeaters logging data to the same MQTT server so
you can easily troubleshoot packets through the mesh. You will need to build a
custom image with packet logging and/or debug for your repeater to view the
data.

One way of tracking a message through the mesh is filtering the MQTT data on the
hash field as each message has a unique hash. You can see which repeaters the
message hits!

## Quick Install

### One-Line Installation (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | sudo bash
```

The installer will:

- Create a dedicated `mctomqtt` system user
- Install to `/opt/mctomqtt/` with config at `/etc/mctomqtt/`
- Guide you through interactive MQTT broker configuration
- Set up Python virtual environment (requires Python 3.11+)
- Configure a systemd service (Linux) or launchd daemon (macOS)
- Auto-detect and migrate existing `~/.meshcoretomqtt` installations

### Custom Repository/Branch

Install from a fork or custom branch:

```bash
curl -fsSL https://raw.githubusercontent.com/yourusername/meshcoretomqtt/yourbranch/install.sh | \
  sudo bash -s -- --repo yourusername/meshcoretomqtt --branch yourbranch
```

### Local Testing

```bash
git clone https://github.com/Cisien/meshcoretomqtt
cd meshcoretomqtt
sudo LOCAL_INSTALL=$(pwd) ./install.sh
```

### NixOS

flake.nix

```nix
inputs = {
  meshcoretomqtt.url = "github:Cisien/meshcoretomqtt"
};
```

in your system config

```nix
imports = [inputs.meshcoretomqtt.nixosModules.default];
services.mctomqtt = {
    enable = true;
    iata = "FOO";
    serialPorts = ["/dev/ttyUSB0"];

    # Disable defaults if you like.
    # Defaults are used if nothing is specified
    defaults = {
        letsmesh-us.enable = true;
        letsmesh-eu.enable = true;
    };

    # Define custom brokers if you need them
    brokers = [
      {
        name = "my-broker";
        enabled = true;
        server = "mqtt.example.com";
        port = 1883;
        tls.enabled = true;
        auth = {
          method = "password";
          username = "my_username";
          password = "my_password";
        };
      }
    ];

    # Additional settings
    settings = {
      log-level = "DEBUG";
    };
  };
```

## Prerequisites

### Hardware Setup

1. Setup a Raspberry Pi (Zero / 2 / 3 or 4 recommended) or similar Linux/macOS
   device
2. Build/flash a MeshCore repeater with appropriate build flags:

   **Recommended minimum:**
   ```
   -D MESH_PACKET_LOGGING=1
   ```

   **Optional debug data:**
   ```
   -D MESH_DEBUG=1
   ```

3. Plug the repeater into the device via USB (RAK or Heltec tested)
4. Configure the repeater with a unique name as per MeshCore guides

### Software Requirements

- Python 3.11 or higher (required for `tomllib` stdlib module)
- For auth token support (optional): Node.js and `@michaelhart/meshcore-decoder`

The installer handles these dependencies automatically!

## Directory Layout

```
/opt/mctomqtt/              # App home (owned by mctomqtt:mctomqtt)
  mctomqtt.py               # Entry point
  bridge/                   # Core bridge package
  auth_token.py
  config_loader.py
  .version_info
  venv/                     # Python venv (pyserial, paho-mqtt)
  .nvm/                     # NVM + Node LTS + meshcore-decoder

/etc/mctomqtt/              # Config (owned root:mctomqtt, 755)
  config.toml               # Defaults (644, OVERWRITTEN on updates)
  config.d/                 # Drop-in override directory
    00-user.toml               # User config (644, never overwritten)
```

## Configuration

Configuration uses TOML files with a layered override system:

- `/etc/mctomqtt/config.toml` — Default values (overwritten on updates, do not edit)
- `/etc/mctomqtt/config.d/00-user.toml` — Your custom configuration (never overwritten)

Files in `config.d/` are loaded alphabetically and deep-merged over the defaults.

To bypass the default config loading entirely, use `--config`:

```bash
mctomqtt.py --config /path/to/config.toml
mctomqtt.py --config /path/to/base.toml --config /path/to/overrides.toml
```

When `--config` is used, `/etc/mctomqtt/` is not read. Multiple `--config` flags are supported; files are loaded in order with later files overlaying earlier ones.

### Editing Configuration

```bash
sudo nano /etc/mctomqtt/config.d/00-user.toml
```

### Basic Example (00-user.toml)

```toml
[general]
iata = "SEA"

[serial]
ports = ["/dev/ttyACM0"]

[[broker]]
name = "my-mqtt"
enabled = true
server = "mqtt.example.com"
port = 1883

[broker.auth]
method = "password"
username = "my_username"
password = "my_password"
```

### Advanced Example with Multiple Brokers

```toml
[general]
iata = "SEA"

[serial]
ports = ["/dev/ttyACM0"]

# Local MQTT with Username/Password
[[broker]]
name = "local-mqtt"
enabled = true
server = "mqtt.local"
port = 1883

[broker.auth]
method = "password"
username = "localuser"
password = "localpass"

# LetsMesh.net Packet Analyzer (US)
[[broker]]
name = "letsmesh-us"
enabled = true
server = "mqtt-us-v1.letsmesh.net"
port = 443
transport = "websockets"

[broker.tls]
enabled = true

[broker.auth]
method = "token"
audience = "mqtt-us-v1.letsmesh.net"
```

### Topic Templates

Topics support template variables:

- `{IATA}` — Your 3-letter location code
- `{PUBLIC_KEY}` — Device public key (auto-detected)

**Global topics** (in config.toml defaults):

```toml
[topics]
status = "meshcore/{IATA}/{PUBLIC_KEY}/status"
packets = "meshcore/{IATA}/{PUBLIC_KEY}/packets"
debug = "meshcore/{IATA}/{PUBLIC_KEY}/debug"
```

**Per-broker topic overrides** (optional):

```toml
[[broker]]
name = "custom-broker"
enabled = true
server = "mqtt.example.com"

[broker.topics]
status = "custom/{IATA}/{PUBLIC_KEY}/status"
iata = "LAX"
```

## Authentication Methods

### 1. Username/Password

```toml
[[broker]]
name = "my-broker"
enabled = true
server = "mqtt.example.com"

[broker.auth]
method = "password"
username = "your_username"
password = "your_password"
```

### 2. Auth Token (Public Key Based)

Requires `@michaelhart/meshcore-decoder` and firmware supporting `get prv.key`
command.

```toml
[[broker]]
name = "letsmesh-us"
enabled = true
server = "mqtt-us-v1.letsmesh.net"
port = 443
transport = "websockets"

[broker.tls]
enabled = true

[broker.auth]
method = "token"
audience = "mqtt-us-v1.letsmesh.net"
```

The script will:

- Read the private key from the connected MeshCore device via serial
- Generate JWT auth tokens using the device's private key
- Authenticate using the `v1_{PUBLIC_KEY}` username format

**Note:** The private key is read directly from the device and used for signing
only. It's never transmitted or saved to disk.

To install meshcore-decoder:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
# Restart shell or: source ~/.bashrc
nvm install --lts
npm install -g @michaelhart/meshcore-decoder
```

### Additional Settings

```toml
[general]
# Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL
log_level = "INFO"

# Wait for system clock sync before setting repeater time (default: true)
# Set to false on systems without timedatectl or NTP
sync_time = true
```

### Remote Serial (LetsMesh.net Experimental)

Remote serial allows you to execute serial commands on your node remotely via
the LetsMesh.net MeshCore Packet Analyzer web interface. Commands are cryptographically
signed by an authorized companion device connected via Bluetooth.

**Security Model:**
- Commands must be signed with an Ed25519 private key
- Only companions in the allowlist can send commands
- Each command JWT has a 30-second expiry (checked against system clock)
- Nonces prevent replay attacks
- Responses are signed by the node's private key for end-to-end verification

**Configuration:**

```toml
[remote_serial]
enabled = true
allowed_companions = [
    "03CEBEA3DA9C279CF8EB9449F0CC5BA3690621EE66A3B91067CDBA881EC883A5"
]
nonce_ttl = 120
command_timeout = 10
```

**How it works:**
1. You connect your companion device via Bluetooth to the Packet Analyzer web interface
2. The browser uses the companion's private key to sign command JWTs
3. Commands are sent via MQTT to your node's `serial/commands` topic
4. This script verifies the JWT signature against the allowlist
5. Valid commands are executed on the serial port
6. Responses are signed and published to the `serial/responses` topic

**Note:** Ensure your system clock is synchronized (NTP) for JWT expiry verification.

## Running the Script

The installer offers three deployment options:

### 1. System Service (Recommended)

Automatically starts on boot and runs as a dedicated system user.

**Linux (systemd):**

```bash
sudo systemctl start mctomqtt      # Start service
sudo systemctl stop mctomqtt       # Stop service
sudo systemctl status mctomqtt     # Check status
sudo systemctl restart mctomqtt    # Restart service
sudo journalctl -u mctomqtt -f     # View logs
```

**macOS (launchd):**

```bash
sudo launchctl load /Library/LaunchDaemons/com.meshcore.mctomqtt.plist
sudo launchctl unload /Library/LaunchDaemons/com.meshcore.mctomqtt.plist
sudo launchctl list | grep mctomqtt
tail -f /var/log/mctomqtt.log
```

### 2. Docker Container

```bash
# Build the image
docker build -t mctomqtt:latest /path/to/meshcoretomqtt

# Run the container
docker run -d \
  --name mctomqtt \
  --restart unless-stopped \
  -v /path/to/config.toml:/etc/mctomqtt/config.toml \
  --device=/dev/ttyACM0 \
  mctomqtt:latest
```

### 3. Manual Execution

```bash
cd /opt/mctomqtt
sudo -u mctomqtt ./venv/bin/python3 mctomqtt.py --config /etc/mctomqtt/config.toml
```

With debug output:

```bash
sudo -u mctomqtt ./venv/bin/python3 mctomqtt.py --config /etc/mctomqtt/config.toml --debug
```

## Updates

Use the standalone update script for the simplest update experience:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/scripts/update.sh | sudo bash
```

Or re-run the installer — it will detect your existing installation and offer to update:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | sudo bash
```

For non-interactive updates:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | sudo bash -s -- --update
```

The updater will:

- Detect your existing service type (systemd/launchd/Docker)
- Stop the service/container
- Download and verify updated files
- Overwrite `/etc/mctomqtt/config.toml` with latest defaults
- Preserve your `/etc/mctomqtt/config.d/00-user.toml` configuration
- Restart the service/container automatically

## Migration

If you have a legacy `~/.meshcoretomqtt` installation, migrate to the new layout:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/scripts/migrate.sh | sudo bash
```

The migrator will:

- Convert `.env`/`.env.local` configuration to TOML format
- Stop and remove old systemd/launchd services
- Preserve the old installation directory for manual cleanup

## Uninstallation

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/uninstall.sh | sudo bash
```

The uninstaller will:

- Stop and remove the service
- Offer to backup your `00-user.toml` configuration
- Remove `/opt/mctomqtt/` and `/etc/mctomqtt/`
- Remove the `mctomqtt` system user

## Privacy

This tool collects and forwards all packets transmitted over the MeshCore
network. Privacy on MeshCore is provided by protecting secret channel 
keys. All packets will be forwarded as raw data without additional processing
or decryption. The primary use of this script is to send data to LetsMesh.net.
Learn at https://letsmesh.net/

## Viewing the data

- Use a MQTT tool to view the packet data. I recommend MQTTX.
- Data will appear in topics based on your configuration. Default format:
  ```
  meshcore/{IATA}/{PUBLIC_KEY}/status
  meshcore/{IATA}/{PUBLIC_KEY}/packets
  meshcore/{IATA}/{PUBLIC_KEY}/debug
  ```
  Where `{IATA}` is your 3-letter location code and `{PUBLIC_KEY}` is your
  device's public key (auto-detected).

  **status**: Last will and testament (LWT) showing online/offline status.

  **packets**: Flood or direct packets going through the repeater.

  **debug**: Debug info (if enabled on the repeater build).

## Example MQTT data...

Note: origin is the repeater node reporting the data to mqtt. Not the origin of
the LoRa packet.

Flood packet...

```
Topic: meshcore/SEA/A1B2.../packets QoS: 0
{"origin": "ag loft rpt", "origin_id": "A1B2...", "timestamp": "2025-03-16T00:07:11.191561", "type": "PACKET", "direction": "rx", "time": "00:07:09", "date": "16/3/2025", "len": "87", "packet_type": "5", "route": "F", "payload_len": "83", "raw": "0A1B2C...", "SNR": "4", "RSSI": "-93", "score": "1000", "hash": "AC9D2DDDD8395712"}
```

Direct packet...

```
Topic: meshcore/SEA/A1B2.../packets QoS: 0
{"origin": "ag loft rpt", "origin_id": "A1B2...", "timestamp": "2025-03-15T23:09:00.710459", "type": "PACKET", "direction": "rx", "time": "23:08:59", "date": "15/3/2025", "len": "22", "packet_type": "2", "route": "D", "payload_len": "20", "raw": "0A1B2C...", "SNR": "5", "RSSI": "-93", "score": "1000", "hash": "890BFA3069FD1250", "path": "C2 -> E2"}
```

