"""Migration from legacy ~/.meshcoretomqtt to /opt/mctomqtt."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .system import download_repo_archive, run_cmd
from .ui import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt_yes_no,
)

if TYPE_CHECKING:
    from . import InstallerContext


# ---------------------------------------------------------------------------
# .env file parsing and TOML conversion (ported from embedded Python in bash)
# ---------------------------------------------------------------------------

def parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict."""
    env: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def env_to_toml(env: dict[str, str]) -> str:
    """Convert a merged .env dict to TOML config string."""
    lines: list[str] = []

    # General section
    general: dict[str, str] = {}
    iata = env.get("MCTOMQTT_IATA", "")
    if iata and iata != "XXX":
        general["iata"] = iata
    log_level = env.get("MCTOMQTT_LOG_LEVEL", "")
    if log_level and log_level != "INFO":
        general["log_level"] = log_level
    sync_time = env.get("MCTOMQTT_SYNC_TIME", "")
    if sync_time and sync_time.lower() != "true":
        general["sync_time"] = sync_time.lower()

    if general:
        lines.append("[general]")
        for k, v in general.items():
            if v in ("true", "false"):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{v}"')
        lines.append("")

    # Serial section
    ports = env.get("MCTOMQTT_SERIAL_PORTS", "")
    baud = env.get("MCTOMQTT_SERIAL_BAUD_RATE", "")
    timeout = env.get("MCTOMQTT_SERIAL_TIMEOUT", "")
    if ports or baud or timeout:
        lines.append("[serial]")
        if ports:
            port_list = [p.strip() for p in ports.split(",") if p.strip()]
            ports_str = ", ".join(f'"{p}"' for p in port_list)
            lines.append(f"ports = [{ports_str}]")
        if baud and baud != "115200":
            lines.append(f"baud_rate = {baud}")
        if timeout and timeout != "2":
            lines.append(f"timeout = {timeout}")
        lines.append("")

    # Update section
    repo = env.get("MCTOMQTT_UPDATE_REPO", "")
    branch = env.get("MCTOMQTT_UPDATE_BRANCH", "")
    if repo or branch:
        lines.append("[update]")
        if repo:
            lines.append(f'repo = "{repo}"')
        if branch:
            lines.append(f'branch = "{branch}"')
        lines.append("")

    # Remote serial
    rs_enabled = env.get("MCTOMQTT_REMOTE_SERIAL_ENABLED", "false")
    rs_companions = env.get("MCTOMQTT_REMOTE_SERIAL_ALLOWED_COMPANIONS", "")
    if rs_enabled == "true" or rs_companions:
        lines.append("[remote_serial]")
        lines.append(f"enabled = {rs_enabled}")
        if rs_companions:
            companion_list = [c.strip() for c in rs_companions.split(",") if c.strip()]
            comp_str = ", ".join(f'"{c}"' for c in companion_list)
            lines.append(f"allowed_companions = [{comp_str}]")
        else:
            lines.append("allowed_companions = []")
        lines.append("")

    # Brokers
    for broker_num in range(1, 5):
        prefix = f"MCTOMQTT_MQTT{broker_num}_"
        enabled = env.get(f"{prefix}ENABLED", "false")
        server = env.get(f"{prefix}SERVER", "")
        if enabled != "true" or not server:
            continue

        port_val = env.get(f"{prefix}PORT", "1883")
        transport = env.get(f"{prefix}TRANSPORT", "tcp")
        use_tls = env.get(f"{prefix}USE_TLS", "false")
        tls_verify = env.get(f"{prefix}TLS_VERIFY", "true")
        use_auth_token = env.get(f"{prefix}USE_AUTH_TOKEN", "false")
        username = env.get(f"{prefix}USERNAME", "")
        password = env.get(f"{prefix}PASSWORD", "")
        token_audience = env.get(f"{prefix}TOKEN_AUDIENCE", "")
        token_owner = env.get(f"{prefix}TOKEN_OWNER", "")
        token_email = env.get(f"{prefix}TOKEN_EMAIL", "")
        keepalive = env.get(f"{prefix}KEEPALIVE", "60")
        qos = env.get(f"{prefix}QOS", "0")
        retain = env.get(f"{prefix}RETAIN", "true")

        if "letsmesh" in server:
            if "-us-" in server:
                broker_name = "letsmesh-us"
            elif "-eu-" in server:
                broker_name = "letsmesh-eu"
            else:
                broker_name = f"letsmesh-{broker_num}"
        else:
            broker_name = f"custom-{broker_num}"

        lines.append("[[broker]]")
        lines.append(f'name = "{broker_name}"')
        lines.append("enabled = true")
        lines.append(f'server = "{server}"')
        lines.append(f"port = {port_val}")
        lines.append(f'transport = "{transport}"')
        lines.append(f"keepalive = {keepalive}")
        lines.append(f"qos = {qos}")
        lines.append(f"retain = {retain}")
        lines.append("")

        if use_tls == "true":
            lines.append("[broker.tls]")
            lines.append("enabled = true")
            lines.append(f"verify = {tls_verify}")
            lines.append("")

        lines.append("[broker.auth]")
        if use_auth_token == "true":
            lines.append('method = "token"')
            if token_audience:
                lines.append(f'audience = "{token_audience}"')
            if token_owner:
                lines.append(f'owner = "{token_owner}"')
            if token_email:
                lines.append(f'email = "{token_email}"')
        elif username:
            lines.append('method = "password"')
            lines.append(f'username = "{username}"')
            lines.append(f'password = "{password}"')
        else:
            lines.append('method = "none"')
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Migration command
# ---------------------------------------------------------------------------

def _real_user_home() -> Path:
    """Get the real user's home directory, even when running under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def detect_old_installation() -> str | None:
    """Check for legacy ~/.meshcoretomqtt installation. Returns path or None."""
    old_dir = _real_user_home() / ".meshcoretomqtt"
    if old_dir.is_dir() and (old_dir / "mctomqtt.py").exists():
        return str(old_dir)
    return None


_MIGRATED_SENTINEL = ".migrated"


def is_already_migrated(old_dir: str) -> bool:
    """Return True if the legacy directory has already been migrated."""
    return (Path(old_dir) / _MIGRATED_SENTINEL).exists()


def mark_migrated(old_dir: str, install_dir: str) -> None:
    """Write a sentinel file so future migration runs are skipped."""
    (Path(old_dir) / _MIGRATED_SENTINEL).write_text(
        f"Migrated to {install_dir}\n"
    )


def run_migrate(ctx: InstallerContext) -> bool:
    """Migrate from ~/.meshcoretomqtt to /opt/mctomqtt.

    Returns True if migration was performed, False if skipped/nothing to migrate.
    """
    old_dir = detect_old_installation()
    if old_dir is None:
        return False

    if is_already_migrated(old_dir):
        print_info("Legacy installation already migrated. Skipping.")
        return False

    print()
    print_header("Legacy Installation Detected")
    print_info(f"Found existing installation at: {old_dir}")
    print()

    if not prompt_yes_no(f"Migrate to new installation at {ctx.install_dir}?", "y"):
        print_info("Skipping migration. Old installation left in place.")
        return False

    # Step 1: Stop old services
    _stop_old_services(old_dir)

    # Step 2: Migrate config
    print_info("Migrating configuration to TOML format...")

    old_env = os.path.join(old_dir, ".env")
    old_env_local = os.path.join(old_dir, ".env.local")

    # Get repo's default .env for diffing
    repo_defaults: dict[str, str] = {}
    if os.path.exists(old_env):
        # Use already-downloaded repo archive if available, otherwise download it
        repo_dir = ctx.repo_dir
        if not repo_dir and ctx.local_install:
            repo_dir = ctx.local_install
        if not repo_dir:
            try:
                migrate_tmp = tempfile.mkdtemp()
                repo_dir = download_repo_archive(ctx.repo, ctx.branch, migrate_tmp)
                ctx.repo_dir = repo_dir
            except subprocess.CalledProcessError:
                repo_dir = ""

        if repo_dir:
            default_env = os.path.join(repo_dir, ".env")
            if os.path.exists(default_env):
                repo_defaults = parse_env_file(default_env)

    # Parse user's env files
    user_env = parse_env_file(old_env)
    user_env_local = parse_env_file(old_env_local)

    # Detect customizations in .env vs repo default
    env_customizations: dict[str, str] = {}
    for key, value in user_env.items():
        if key in repo_defaults and repo_defaults[key] != value:
            env_customizations[key] = value
        elif key not in repo_defaults:
            env_customizations[key] = value

    # Merge: env customizations + env.local overrides
    merged: dict[str, str] = {}
    merged.update(env_customizations)
    merged.update(user_env_local)

    # Create config directory
    os.makedirs(f"{ctx.config_dir}/config.d", exist_ok=True)

    migrated_toml_path = f"{ctx.config_dir}/config.d/00-user.toml"

    if not merged:
        print_warning("No user configuration found to migrate")
    else:
        toml_content = env_to_toml(merged)

        # Write directly
        Path(migrated_toml_path).write_text(
            "# MeshCore to MQTT - User Configuration\n"
            "# Migrated from legacy .env/.env.local installation\n\n"
            + toml_content
        )

        if os.path.exists(migrated_toml_path):
            print_success(f"Configuration migrated to {migrated_toml_path}")
            print()
            print_info("Migrated configuration:")
            content = Path(migrated_toml_path).read_text()
            print(content)
            print()
        else:
            print_warning("No configuration was migrated (no user overrides found)")

    # Step 3: Remove old systemd unit
    _cleanup_old_service_units()

    # Step 4: Mark migration as complete
    mark_migrated(old_dir, ctx.install_dir)

    # Step 5: Inform user about old directory
    print()
    print_info(f"Old installation preserved at: {old_dir}")
    print_info("You can remove it once you've verified the new installation works:")
    print(f"  rm -rf {old_dir}")
    print()

    return True


def _stop_old_services(old_dir: str) -> None:
    """Stop and disable old systemd/launchd services."""
    if Path("/etc/systemd/system/mctomqtt.service").exists():
        print_info("Stopping old systemd service...")
        run_cmd(["systemctl", "stop", "mctomqtt.service"], check=False)
        run_cmd(["systemctl", "disable", "mctomqtt.service"], check=False)
        print_success("Old service stopped and disabled")

    if platform.system() == "Darwin":
        old_plist = _real_user_home() / "Library" / "LaunchAgents" / "com.meshcore.mctomqtt.plist"
        if old_plist.exists():
            print_info("Stopping old launchd service...")
            run_cmd(["launchctl", "unload", str(old_plist)], check=False)
            print_success("Old launchd service stopped")


def _cleanup_old_service_units() -> None:
    """Remove old systemd/launchd unit files."""
    if Path("/etc/systemd/system/mctomqtt.service").exists():
        print_info("Removing old systemd unit...")
        Path("/etc/systemd/system/mctomqtt.service").unlink(missing_ok=True)
        run_cmd(["systemctl", "daemon-reload"])
        print_success("Old systemd unit removed")

    if platform.system() == "Darwin":
        old_plist = _real_user_home() / "Library" / "LaunchAgents" / "com.meshcore.mctomqtt.plist"
        if old_plist.exists():
            os.unlink(old_plist)
            print_success("Old launchd plist removed")
