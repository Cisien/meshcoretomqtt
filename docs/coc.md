# Bluetooth repeater logs

The `coc` transport receives a repeater's packet logs over Bluetooth LE. It uses
the same parser and MQTT configuration as the USB serial transport.

## Before you start

This setup requires Linux with BlueZ and systemd. The prebuilt helper runs on
Debian 12, Raspberry Pi OS Bookworm and newer glibc-based systems.

The repeater needs the [CoC firmware](https://github.com/throny/meshcore-ble-observer/releases/tag/v1.18.0-coc)
and must be [enrolled with the receiving computer](https://github.com/throny/meshcore-ble-observer/blob/main/docs/enrollment.md).
Enrollment is done through the repeater's USB console before starting the bridge.

CoC support is currently in [PR #74](https://github.com/Cisien/meshcoretomqtt/pull/74),
not the upstream release. Install the
[`feature/repeater-observer-coc-20261029` branch](https://github.com/throny/meshcoretomqtt/tree/feature/repeater-observer-coc-20261029)
using the installer's [custom repository and branch options](../README.md#custom-repositorybranch).
Choose the system service on Linux. An existing CoC installation does not need
to be reinstalled.

## Install the helper

Download the executable for your computer and `SHA256SUMS` from the
[v0.1.0 release](https://github.com/throny/meshcoretomqtt/releases/tag/coc-helper-v0.1.0).
Put both files in the same directory.

| Linux system | Executable |
| --- | --- |
| 64-bit Raspberry Pi or ARM64 | `observer-coc-helper-aarch64` |
| 32-bit Raspberry Pi or ARMv7 | `observer-coc-helper-armv7` |
| Intel or AMD, 64-bit | `observer-coc-helper-x86_64` |

Use `getconf LONG_BIT` to check whether your Pi OS is 32-bit or 64-bit.
Verify the download, then install it. The example below uses the ARM64 file:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

Once the checksum check succeeds:

```sh
sudo install -D -o root -g root -m 0755 observer-coc-helper-aarch64 \
  /usr/local/libexec/meshcoretomqtt/observer-coc-helper
```

## Configure meshcoretomqtt

Edit `/etc/mctomqtt/config.d/99-user.toml`. Set `transport` in the existing
`[general]` section and add or update `[coc]` as follows. Use the repeater's
Bluetooth address and leave your broker configuration in place.

```toml
[general]
transport = "coc"

[coc]
address = "02:00:00:00:00:01"
adapter = "hci0"
helper = "/usr/local/libexec/meshcoretomqtt/observer-coc-helper"
data_socket = "/run/mctomqtt-coc/data.sock"
control_socket = "/run/mctomqtt-coc/control.sock"
```

The service needs a directory for the helper's sockets. Run
`sudo systemctl edit mctomqtt.service` and add:

```ini
[Service]
RuntimeDirectory=mctomqtt-coc
RuntimeDirectoryMode=0700
```

systemd creates this directory when the service starts. meshcoretomqtt starts
the helper, so no separate helper service is needed.

## Start the bridge

Stop any other receiver connected to this repeater, then restart meshcoretomqtt:

```sh
sudo systemctl daemon-reload
sudo systemctl restart mctomqtt.service
journalctl -u mctomqtt.service -n 30 --no-pager
```

Check the log for a Bluetooth connection, then check that packets reach your
MQTT broker.

## Clock synchronization

With `general.sync_time = true`, meshcoretomqtt sets the repeater's clock from
the Linux host when it connects. The host must have synchronized its own clock
first. Set `sync_time = false` if you manage the repeater's time from a Companion.

## Troubleshooting

- **The helper does not start:** check the executable path, architecture and OS
  version. The release builds use glibc 2.36 as their supported baseline.
- **Bluetooth does not connect:** check the peer address, `hci0` adapter and
  enrollment. The discovery fallback needs `gatttool` and `timeout` installed.
- **A clock update is rejected:** MeshCore refuses to move the clock backwards.

For development or an unsupported host, see [Building the helper](https://github.com/throny/meshcoretomqtt/blob/main/coc-helper/RELEASING.md).
