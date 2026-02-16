"""System operations: subprocess wrappers, user/service management, serial detection."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .ui import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt_input,
    prompt_yes_no,
)

if TYPE_CHECKING:
    from . import InstallerContext


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def run_cmd(
    cmd: list[str] | str,
    *,
    check: bool = True,
    capture: bool = False,
    shell: bool = False,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a command, optionally capturing output."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        shell=shell,
        **kwargs,
    )


def require_root() -> None:
    """Verify the installer is running as root."""
    if os.getuid() != 0:
        print_error("This installer must be run as root.")
        print_info("Re-run with: sudo python3 -m installer ...")
        raise SystemExit(1)


def chown_recursive(path: str, user: str, group: str) -> None:
    """Recursively chown a directory tree."""
    shutil.chown(path, user, group)
    for dirpath, dirnames, filenames in os.walk(path):
        for d in dirnames:
            shutil.chown(os.path.join(dirpath, d), user, group)
        for f in filenames:
            shutil.chown(os.path.join(dirpath, f), user, group)


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def download_file(url: str, dest: str, name: str) -> None:
    """Download a file with curl and retry."""
    print_info(f"Downloading {name}...")
    run_cmd(
        ["curl", "-fsSL", "--retry", "3", "--retry-delay", "2", url, "-o", dest],
        check=True,
    )


def download_repo_archive(repo: str, branch: str, dest_dir: str) -> str:
    """Download and extract a GitHub repo zip archive.

    Downloads the archive from GitHub, extracts it, and returns the path
    to the extracted repo root directory.

    Args:
        repo: GitHub repo in "owner/name" format.
        branch: Branch or tag name to download.
        dest_dir: Directory to extract into.

    Returns:
        Path to the extracted repo root (e.g., dest_dir/meshcoretomqtt-main/).
    """
    # TODO: Switch to downloading GitHub Releases once CI/CD is set up
    # to create tagged releases. For now, download the branch archive.
    archive_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
    zip_path = os.path.join(dest_dir, "repo.zip")

    print_info(f"Downloading repository archive ({repo} @ {branch})...")
    run_cmd(
        ["curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
         "-o", zip_path, archive_url],
        check=True,
    )

    print_info("Extracting archive...")
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)

    os.unlink(zip_path)

    # GitHub archives extract to {repo_name}-{branch}/
    repo_name = repo.split("/")[-1]
    extracted_dir = os.path.join(dest_dir, f"{repo_name}-{branch}")
    if not os.path.isdir(extracted_dir):
        # Fallback: find the single extracted directory
        entries = [
            e for e in os.listdir(dest_dir)
            if os.path.isdir(os.path.join(dest_dir, e))
        ]
        if len(entries) == 1:
            extracted_dir = os.path.join(dest_dir, entries[0])
        else:
            raise RuntimeError(
                f"Could not determine extracted directory in {dest_dir}: {entries}"
            )

    print_success("Repository archive extracted")
    return extracted_dir


# ---------------------------------------------------------------------------
# Service user management
# ---------------------------------------------------------------------------

def prompt_service_user(ctx: InstallerContext) -> str:
    """Prompt for the service account username and return it."""
    default = ctx.svc_user
    # Try to read existing user from systemd unit
    unit_path = Path("/etc/systemd/system/mctomqtt.service")
    if unit_path.exists():
        for line in unit_path.read_text().splitlines():
            if line.startswith("User="):
                default = line.split("=", 1)[1].strip()
                break

    username = prompt_input("Service account username", default)
    # Sanitize: lowercase, strip spaces
    username = username.lower().replace(" ", "")
    return username or default


def create_system_user(svc_user: str, install_dir: str) -> None:
    """Create a system user for the service (Linux only)."""
    # Check if user already exists
    result = run_cmd(["id", svc_user], check=False, capture=True)
    if result.returncode == 0:
        print_success(f"Service user '{svc_user}' already exists")
    else:
        print_info(f"Creating system user '{svc_user}'...")
        run_cmd([
            "useradd", "--system", "--no-create-home",
            "--shell", "/usr/sbin/nologin",
            "--home-dir", install_dir,
            svc_user,
        ])
        print_success(f"System user '{svc_user}' created")

    # Add to serial device group
    if platform.system() == "Linux":
        is_arch = Path("/etc/arch-release").exists()
        group = "uucp" if is_arch else "dialout"
        # Check group exists
        result = run_cmd(["getent", "group", group], check=False, capture=True)
        if result.returncode == 0:
            run_cmd(["usermod", "-aG", group, svc_user])
            print_success(f"Added '{svc_user}' to '{group}' group (serial access)")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def set_permissions(install_dir: str, config_dir: str, svc_user: str) -> None:
    """Set directory ownership and permissions."""
    # /opt/mctomqtt owned by svc_user:svc_user
    chown_recursive(install_dir, svc_user, svc_user)
    print_success(f"{install_dir} owned by {svc_user}:{svc_user}")

    # /etc/mctomqtt owned by root:svc_user, mode 750
    chown_recursive(config_dir, "root", svc_user)
    os.chmod(config_dir, 0o750)
    config_d = Path(config_dir) / "config.d"
    if config_d.exists():
        os.chmod(str(config_d), 0o750)

    config_toml = Path(config_dir) / "config.toml"
    if config_toml.exists():
        os.chmod(str(config_toml), 0o640)

    user_toml = Path(config_dir) / "config.d" / "00-user.toml"
    if user_toml.exists():
        os.chmod(str(user_toml), 0o640)

    print_success(f"Permissions set on {config_dir} (root:{svc_user}, 750/640)")


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker_cmd() -> str | None:
    """Return 'docker' if the daemon is reachable, or None."""
    result = run_cmd(["docker", "info"], check=False, capture=True)
    if result.returncode == 0:
        return "docker"
    return None


# ---------------------------------------------------------------------------
# Serial device detection
# ---------------------------------------------------------------------------

def detect_serial_devices() -> list[str]:
    """Detect available serial devices."""
    devices: list[str] = []

    if platform.system() == "Darwin":
        # macOS: Use /dev/cu.* devices
        for pattern in ("cu.usb*", "cu.wchusbserial*", "cu.SLAB_USBtoUART*"):
            devices.extend(str(p) for p in sorted(Path("/dev").glob(pattern)))
    else:
        # Linux: Prefer /dev/serial/by-id/ for persistent naming
        by_id = Path("/dev/serial/by-id")
        if by_id.is_dir():
            devices.extend(str(p) for p in sorted(by_id.iterdir()))

        # Also check /dev/ttyACM* and /dev/ttyUSB*
        resolved_existing = set()
        for d in devices:
            try:
                resolved_existing.add(str(Path(d).resolve()))
            except OSError:
                pass

        for pattern in ("ttyACM*", "ttyUSB*"):
            for p in sorted(Path("/dev").glob(pattern)):
                if str(p.resolve()) not in resolved_existing:
                    devices.append(str(p))

    return devices


def select_serial_device() -> str:
    """Interactive device selection. Returns selected path."""
    devices = detect_serial_devices()

    print()
    print_header("Serial Device Selection")
    print()

    if not devices:
        print_warning("No serial devices detected")
        print()
        print("  1) Enter path manually")
        print()
        prompt_input("Select option [1]", "1")
        return prompt_input("Enter serial device path", "/dev/ttyACM0")

    label = "1 serial device" if len(devices) == 1 else f"{len(devices)} serial devices"
    print_info(f"Found {label}:")
    print()

    for i, device in enumerate(devices, 1):
        if platform.system() != "Darwin" and device.startswith("/dev/serial/by-id/"):
            try:
                resolved = str(Path(device).resolve())
                info = f"{device} -> {resolved}"
            except OSError:
                info = device
        else:
            info = device
        print(f"  {i}) {info}")

    manual_idx = len(devices) + 1
    print(f"  {manual_idx}) Enter path manually")
    print()

    while True:
        choice = prompt_input(f"Select device [1-{manual_idx}]", "1")
        if choice.isdigit() and 1 <= int(choice) <= manual_idx:
            idx = int(choice)
            if idx == manual_idx:
                return prompt_input("Enter serial device path", "/dev/ttyACM0")
            return devices[idx - 1]
        print_error(f"Invalid selection. Please enter a number between 1 and {manual_idx}")


# ---------------------------------------------------------------------------
# System type detection
# ---------------------------------------------------------------------------

def detect_system_type(install_dir: str) -> str:
    """Detect installation type from marker file or running services."""
    marker = Path(install_dir) / ".install_type"
    if marker.exists():
        return marker.read_text().strip()

    # Fallback: detect from running services
    docker = docker_cmd()
    if docker:
        result = run_cmd(
            ["docker", "ps", "-a"],
            check=False, capture=True,
        )
        if result.returncode == 0 and "mctomqtt" in result.stdout:
            return "docker"

    # systemd
    result = run_cmd(
        ["systemctl", "is-active", "--quiet", "mctomqtt.service"],
        check=False,
    )
    if result.returncode == 0:
        return "systemd"
    if Path("/etc/systemd/system/mctomqtt.service").exists():
        return "systemd"

    # launchd
    if platform.system() == "Darwin":
        result = run_cmd(["launchctl", "list"], check=False, capture=True)
        if result.returncode == 0 and "com.meshcore.mctomqtt" in result.stdout:
            return "launchd"
        if Path("/Library/LaunchDaemons/com.meshcore.mctomqtt.plist").exists():
            return "launchd"

    # Native fallback
    if shutil.which("systemctl"):
        return "systemd"
    if platform.system() == "Darwin":
        return "launchd"
    return "unknown"


def detect_system_type_native() -> str:
    """Detect native system type (ignores existing Docker/services)."""
    if shutil.which("systemctl"):
        return "systemd"
    if platform.system() == "Darwin":
        return "launchd"
    return "unknown"


# ---------------------------------------------------------------------------
# Service health check
# ---------------------------------------------------------------------------

def check_service_health(service_type: str) -> None:
    """Wait briefly and check if the service started successfully."""
    import time

    print_info("Waiting for service to start...")
    time.sleep(5)

    if service_type == "docker":
        result = run_cmd(["docker", "logs", "mctomqtt"], check=False, capture=True)
        ps_result = run_cmd(["docker", "ps"], check=False, capture=True)
        if ps_result.returncode == 0 and "mctomqtt" in ps_result.stdout:
            if "connected to" in result.stdout.lower() or "connected to" in result.stderr.lower():
                print_success("Container started and connected successfully")
            else:
                print_warning("Container started but may not be connected yet")
        print()
        print_info("Recent logs:")
        # Show last 10 lines
        lines = (result.stdout + result.stderr).strip().splitlines()[-10:]
        for line in lines:
            print(f"  {line}")

    elif service_type == "systemd":
        result = run_cmd(
            ["systemctl", "is-active", "--quiet", "mctomqtt.service"],
            check=False,
        )
        log_result = run_cmd(
            ["journalctl", "-u", "mctomqtt.service", "-n", "10", "--no-pager"],
            check=False, capture=True,
        )
        if result.returncode == 0 and "connected to" in log_result.stdout.lower():
            print_success("Service started and connected successfully")
        else:
            print_warning("Service started but may not be connected yet")
        print()
        print_info("Recent logs:")
        for line in log_result.stdout.strip().splitlines()[-10:]:
            print(f"  {line}")

    elif service_type == "launchd":
        result = run_cmd(["launchctl", "list"], check=False, capture=True)
        if "com.meshcore.mctomqtt" in result.stdout:
            print_success("Service started successfully")
        else:
            print_error("Service may not be running")
        print()
        print_info("Recent logs:")
        log_path = Path("/var/log/mctomqtt.log")
        if log_path.exists():
            lines = log_path.read_text().strip().splitlines()[-10:]
            for line in lines:
                print(f"  {line}")
        else:
            print_info("No logs available yet")


# ---------------------------------------------------------------------------
# Service installation
# ---------------------------------------------------------------------------

def install_systemd_service(
    install_dir: str,
    config_dir: str,
    svc_user: str,
    *,
    is_update: bool = False,
    auto: bool = False,
    service_name: str = "mctomqtt",
) -> bool:
    """Install/update a systemd service. Returns True if installed."""
    print_info("Installing systemd service...")

    unit_file = f"{service_name}.service"
    service_exists = False
    service_was_enabled = False
    service_was_running = False
    unit_path = Path(f"/etc/systemd/system/{unit_file}")

    if unit_path.exists():
        service_exists = True
        print_info("Existing service detected - will update")

        r = run_cmd(["systemctl", "is-enabled", unit_file], check=False, capture=True)
        service_was_enabled = r.returncode == 0

        r = run_cmd(["systemctl", "is-active", unit_file], check=False, capture=True)
        if r.returncode == 0:
            service_was_running = True
            print_info("Stopping running service...")
            run_cmd(["systemctl", "stop", unit_file])

    # Generate unit file from template or from scratch
    template_path = Path(install_dir) / "mctomqtt.service"

    if template_path.exists():
        content = template_path.read_text()
        import re
        content = re.sub(r"^User=.*$", f"User={svc_user}", content, flags=re.MULTILINE)
        content = re.sub(r"^Group=.*$", f"Group={svc_user}", content, flags=re.MULTILINE)
        print_info(f"Generated systemd unit from template (User={svc_user})")
    else:
        content = f"""[Unit]
Description=MeshCore to MQTT Relay
After=time-sync.target network-online.target
Wants=time-sync.target network-online.target

[Service]
Type=exec
User={svc_user}
Group={svc_user}
WorkingDirectory={install_dir}
ExecStart={install_dir}/venv/bin/python3 {install_dir}/mctomqtt.py --config {config_dir}/config.toml
Environment="PATH={install_dir}/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin"
Environment="NVM_DIR={install_dir}/.nvm"
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={install_dir}

[Install]
WantedBy=multi-user.target
"""
        print_info(f"Generated systemd unit (User={svc_user})")

    print_info("Installing service file...")
    try:
        unit_path.write_text(content)
    except OSError:
        print_error("Failed to install service file")
        return False

    run_cmd(["systemctl", "daemon-reload"])

    if service_exists:
        if service_was_enabled:
            run_cmd(["systemctl", "enable", unit_file])
            print_success("Service re-enabled")
        if service_was_running:
            print_info("Restarting service...")
            run_cmd(["systemctl", "start", unit_file])
            check_service_health("systemd")
        print_success("Systemd service updated")
    else:
        if auto or prompt_yes_no("Enable service to start on boot?", "y"):
            run_cmd(["systemctl", "enable", unit_file])
            print_success("Service enabled")
        if auto or prompt_yes_no("Start service now?", "y"):
            run_cmd(["systemctl", "start", unit_file])
            check_service_health("systemd")
        print_success("Systemd service installed")

    return True


def install_launchd_service(
    install_dir: str,
    config_dir: str,
    *,
    is_update: bool = False,
    auto: bool = False,
    plist_label: str = "com.meshcore.mctomqtt",
) -> bool:
    """Install a launchd service (macOS). Returns True if installed."""
    print_info("Installing launchd service...")

    plist_dest = f"/Library/LaunchDaemons/{plist_label}.plist"
    template = Path(install_dir) / "com.meshcore.mctomqtt.plist"

    if template.exists():
        print_info("Installing plist from template...")
        shutil.copy2(str(template), plist_dest)
    else:
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{plist_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{install_dir}/venv/bin/python3</string>
        <string>{install_dir}/mctomqtt.py</string>
        <string>--config</string>
        <string>{config_dir}/config.toml</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{install_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{install_dir}/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>NVM_DIR</key>
        <string>{install_dir}/.nvm</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/mctomqtt.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/mctomqtt-error.log</string>
</dict>
</plist>
"""
        Path(plist_dest).write_text(plist_content)

    shutil.chown(plist_dest, "root", "wheel")
    os.chmod(plist_dest, 0o644)

    if auto or prompt_yes_no("Load service now?", "y"):
        run_cmd(["launchctl", "load", plist_dest])
        print_success("Service loaded")

    print_success("Launchd service installed to /Library/LaunchDaemons/")
    return True


def install_docker_service(ctx: InstallerContext) -> bool:
    """Install Docker container. Returns True if installed."""
    print_info("Setting up Docker installation...")

    if not shutil.which("docker"):
        print_error("Docker is not installed. Please install Docker first:")
        print("  macOS: https://docs.docker.com/desktop/install/mac-install/")
        print("  Linux: https://docs.docker.com/engine/install/")
        return False

    docker = docker_cmd()
    if docker is None:
        print_error("Docker daemon is not running. Please start Docker and try again.")
        return False

    result = run_cmd(["docker", "--version"], capture=True)
    print_success(f"Docker found: {result.stdout.strip()}")

    print_header("Building Docker Image")

    dockerfile_path = Path(ctx.install_dir) / "Dockerfile"
    if not dockerfile_path.exists():
        # Copy Dockerfile from repo archive
        if ctx.repo_dir:
            src = Path(ctx.repo_dir) / "Dockerfile"
            if src.exists():
                shutil.copy2(str(src), str(dockerfile_path))
            else:
                print_error("Dockerfile not found in repository archive")
                return False
        else:
            print_error("No repository archive available for Dockerfile")
            return False

    print_info("Building mctomqtt:latest image...")
    print()
    result = run_cmd(["docker", "build", "-t", "mctomqtt:latest", ctx.install_dir], check=False)
    if result.returncode != 0:
        print_error("Failed to build Docker image")
        return False
    print_success("Docker image built successfully")
    print()

    # Get serial device from 00-user.toml
    serial_device = "/dev/ttyACM0"
    user_toml = Path(ctx.config_dir) / "config.d" / "00-user.toml"
    if user_toml.exists():
        import re
        match = re.search(r'^\s*ports\s*=\s*\["([^"]+)"', user_toml.read_text(), re.MULTILINE)
        if match:
            serial_device = match.group(1)

    # Build docker run command
    parts = [
        "docker", "run", "-d", "--name", "mctomqtt", "--restart", "unless-stopped",
        "-v", f"{ctx.config_dir}/config.toml:/etc/mctomqtt/config.toml:ro",
    ]
    if user_toml.exists():
        parts.extend(["-v", f"{ctx.config_dir}/config.d/00-user.toml:/etc/mctomqtt/config.d/00-user.toml:ro"])
    if Path(serial_device).exists():
        parts.append(f"--device={serial_device}")
    else:
        print_warning(f"Serial device {serial_device} not found - container will start but may not connect")
    parts.append("mctomqtt:latest")

    print()
    print_info("Docker run command:")
    print(f"  {' '.join(parts)}")
    print()

    if prompt_yes_no("Start Docker container now?", "y"):
        # Remove existing container if present
        ps_result = run_cmd(["docker", "ps", "-a"], check=False, capture=True)
        if ps_result.returncode == 0 and "mctomqtt" in ps_result.stdout:
            print_info("Removing existing mctomqtt container...")
            run_cmd(["docker", "rm", "-f", "mctomqtt"], check=False)

        result = run_cmd(parts, check=False)
        if result.returncode == 0:
            print_success("Docker container started")
            check_service_health("docker")
        else:
            print_error("Failed to start Docker container")
            return False

    return True


# ---------------------------------------------------------------------------
# Python venv and meshcore-decoder
# ---------------------------------------------------------------------------

def create_venv(install_dir: str, svc_user: str) -> None:
    """Create Python virtual environment and install dependencies."""
    venv_dir = f"{install_dir}/venv"
    venv_python = f"{venv_dir}/bin/python3"

    # Check if existing venv already has required packages
    try:
        result = run_cmd(
            [venv_python, "-c", "import serial, paho.mqtt.client"],
            check=False, capture=True,
        )
        if result.returncode == 0:
            print_success("Using existing virtual environment")
            return
    except FileNotFoundError:
        pass  # venv doesn't exist yet — create it below

    # Create new venv
    shutil.rmtree(venv_dir, ignore_errors=True)

    if svc_user:
        result = run_cmd(
            ["sudo", "-u", svc_user, "python3", "-m", "venv", venv_dir],
            check=False,
        )
        if result.returncode != 0:
            run_cmd(["python3", "-m", "venv", venv_dir])
    else:
        run_cmd(["python3", "-m", "venv", venv_dir])

    print_success(f"Virtual environment created at {venv_dir}")

    print_info("Installing Python dependencies...")
    run_cmd([f"{venv_dir}/bin/pip", "install", "--quiet", "--upgrade", "pip"])
    run_cmd([f"{venv_dir}/bin/pip", "install", "--quiet", "pyserial", "paho-mqtt"])
    print_success("Python dependencies installed (pyserial, paho-mqtt)")


def install_meshcore_decoder(install_dir: str, svc_user: str) -> bool:
    """Install NVM, Node.js LTS, and meshcore-decoder. Returns True on success."""
    print_info(f"Installing NVM and Node.js at {install_dir}/.nvm/...")
    nvm_dir = f"{install_dir}/.nvm"

    if platform.system() == "Darwin" or not svc_user:
        # Install as current user
        env = os.environ.copy()
        env["NVM_DIR"] = nvm_dir
        script = f"""
            export NVM_DIR='{nvm_dir}'
            [ -s "$NVM_DIR/nvm.sh" ] || curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | NVM_DIR='{nvm_dir}' bash
            . "$NVM_DIR/nvm.sh" && nvm install --lts && nvm use --lts
            npm install -g @michaelhart/meshcore-decoder
            NODE_VERSION=$(node --version 2>/dev/null || true)
            if [ -n "$NODE_VERSION" ] && [ -d '{nvm_dir}/versions/node/'"$NODE_VERSION" ]; then
                ln -sfn '{nvm_dir}/versions/node/'"$NODE_VERSION" '{nvm_dir}/versions/node/current'
            fi
        """
        result = run_cmd(["bash", "-c", script], check=False)
    else:
        # Install as service user
        os.makedirs(nvm_dir, exist_ok=True)
        chown_recursive(nvm_dir, svc_user, svc_user)

        script = f"""
            export NVM_DIR='{nvm_dir}'
            [ -s "$NVM_DIR/nvm.sh" ] || curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | NVM_DIR='{nvm_dir}' bash
            . "$NVM_DIR/nvm.sh" && nvm install --lts && nvm use --lts
            npm install -g @michaelhart/meshcore-decoder
            NODE_VERSION=$(node --version 2>/dev/null || true)
            if [ -n "$NODE_VERSION" ] && [ -d '{nvm_dir}/versions/node/'"$NODE_VERSION" ]; then
                ln -sfn '{nvm_dir}/versions/node/'"$NODE_VERSION" '{nvm_dir}/versions/node/current'
            fi
        """
        result = run_cmd(["sudo", "-u", svc_user, "bash", "-c", script], check=False)

    if result.returncode == 0:
        print_success("meshcore-decoder installed")
        return True
    else:
        print_warning("meshcore-decoder installation failed - may require manual install")
        return False


# ---------------------------------------------------------------------------
# Version info
# ---------------------------------------------------------------------------

def create_version_info(ctx: InstallerContext) -> None:
    """Create .version_info JSON file with installer metadata."""
    import urllib.request
    import urllib.error

    git_hash = "unknown"
    api_url = f"https://api.github.com/repos/{ctx.repo}/commits/{ctx.branch}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "meshcoretomqtt-installer"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            git_hash = data.get("sha", "unknown")[:7]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, OSError):
        pass

    info = {
        "installer_version": ctx.script_version,
        "git_hash": git_hash,
        "git_branch": ctx.branch,
        "git_repo": ctx.repo,
        "install_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    version_path = Path(ctx.install_dir) / ".version_info"
    version_path.write_text(json.dumps(info, indent=2) + "\n")

    print_info(f"Version info saved: {ctx.script_version}-{git_hash} ({ctx.repo}@{ctx.branch})")
