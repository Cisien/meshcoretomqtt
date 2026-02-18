"""Update an existing MeshCore to MQTT installation."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import extract_version_from_file
from .config import configure_mqtt_brokers, update_owner_info
from .system import (
    LOCAL_IMAGE,
    check_service_health,
    create_version_info,
    create_venv,
    detect_system_type,
    docker_cmd,
    download_repo_archive,
    install_launchd_service,
    install_meshcore_decoder,
    install_systemd_service,
    pull_or_build_docker_image,
    run_cmd,
    set_permissions,
)
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


def run_update(ctx: InstallerContext) -> None:
    """Update an existing installation."""

    # Verify installation exists
    if not Path(ctx.install_dir, "mctomqtt.py").exists():
        print_error(f"No installation found at {ctx.install_dir}")
        print_info("Run the installer to create a new installation.")
        raise SystemExit(1)

    # Create temp directory for downloads
    tmp_dir = tempfile.mkdtemp()
    try:
        _do_update(ctx, tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _do_update(ctx: InstallerContext, tmp_dir: str) -> None:
    # Download repo archive (or use local install path)
    if ctx.local_install:
        repo_dir = ctx.local_install
    else:
        repo_dir = download_repo_archive(ctx.repo, ctx.branch, tmp_dir)

    ctx.repo_dir = repo_dir

    # Copy mctomqtt.py to extract version
    mctomqtt_tmp = os.path.join(tmp_dir, "mctomqtt.py")
    shutil.copy2(os.path.join(repo_dir, "mctomqtt.py"), mctomqtt_tmp)

    # Extract version
    ctx.script_version = extract_version_from_file(mctomqtt_tmp)

    print_header(f"MeshCore to MQTT Updater v{ctx.script_version}")
    print_info(f"Installation directory: {ctx.install_dir}")
    print_info(f"Configuration directory: {ctx.config_dir}")
    print()

    # Detect existing installation type
    system_type = detect_system_type(ctx.install_dir)
    print_info(f"Detected installation type: {system_type}")

    # ---------------------------------------------------------------------------
    # Install files from repo
    # ---------------------------------------------------------------------------
    print_header("Updating Files")

    if ctx.local_install:
        print_info(f"Installing from local directory: {repo_dir}")
    else:
        print_info(f"Installing from GitHub ({ctx.repo} @ {ctx.branch})...")

    shutil.copy2(os.path.join(repo_dir, "auth_token.py"), os.path.join(tmp_dir, "auth_token.py"))
    shutil.copy2(os.path.join(repo_dir, "config_loader.py"), os.path.join(tmp_dir, "config_loader.py"))
    shutil.copy2(os.path.join(repo_dir, "config.toml.example"), os.path.join(tmp_dir, "config.toml.example"))
    shutil.copy2(os.path.join(repo_dir, "uninstall.sh"), os.path.join(tmp_dir, "uninstall.sh"))
    for f in ("mctomqtt.service", "com.meshcore.mctomqtt.plist"):
        src = os.path.join(repo_dir, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp_dir, f))
    print_success("Files ready")

    # Verify syntax
    print_info("Verifying Python syntax...")
    result = run_cmd(["python3", "-m", "py_compile", mctomqtt_tmp], check=False, capture=True)
    if result.returncode != 0:
        print_error("Syntax errors in mctomqtt.py")
        raise SystemExit(1)

    # Install files
    shutil.copy2(mctomqtt_tmp, f"{ctx.install_dir}/")
    shutil.copy2(os.path.join(tmp_dir, "auth_token.py"), f"{ctx.install_dir}/")
    shutil.copy2(os.path.join(tmp_dir, "config_loader.py"), f"{ctx.install_dir}/")
    # Copy bridge package
    bridge_src = os.path.join(repo_dir, "bridge")
    bridge_dest = os.path.join(ctx.install_dir, "bridge")
    if os.path.isdir(bridge_src):
        if os.path.exists(bridge_dest):
            shutil.rmtree(bridge_dest)
        shutil.copytree(bridge_src, bridge_dest)
    shutil.copy2(os.path.join(tmp_dir, "uninstall.sh"), f"{ctx.install_dir}/")
    for f in ("mctomqtt.service", "com.meshcore.mctomqtt.plist"):
        src = os.path.join(tmp_dir, f)
        if os.path.exists(src):
            shutil.copy2(src, f"{ctx.install_dir}/")
    os.chmod(f"{ctx.install_dir}/mctomqtt.py", 0o755)
    os.chmod(f"{ctx.install_dir}/uninstall.sh", 0o755)

    # Update base config (overwrite config.toml, preserve 00-user.toml)
    shutil.copy2(os.path.join(tmp_dir, "config.toml.example"), f"{ctx.config_dir}/config.toml")
    print_success(f"Base config updated at {ctx.config_dir}/config.toml")
    print_success(f"Files updated in {ctx.install_dir}")

    # ---------------------------------------------------------------------------
    # Update dependencies (skip for Docker)
    # ---------------------------------------------------------------------------
    if system_type != "docker":
        print_header("Updating Dependencies")
        create_venv(ctx.install_dir, ctx.svc_user)

        # Check meshcore-decoder
        result = run_cmd(["which", "meshcore-decoder"], check=False, capture=True)
        if result.returncode == 0:
            ctx.decoder_available = True
        else:
            # Check in NVM path
            nvm_decoder = Path(ctx.install_dir) / ".nvm" / "versions" / "node" / "current" / "bin" / "meshcore-decoder"
            if nvm_decoder.exists():
                ctx.decoder_available = True
    else:
        ctx.decoder_available = True

    # ---------------------------------------------------------------------------
    # Configuration
    # ---------------------------------------------------------------------------
    print_header("Configuration")

    user_toml = Path(ctx.config_dir) / "config.d" / "00-user.toml"
    if user_toml.exists():
        if ctx.update_mode:
            print_info("Keeping existing configuration")
        elif prompt_yes_no("Existing configuration found. Reconfigure?", "n"):
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(str(user_toml), f"{user_toml}.backup-{timestamp}")
            user_toml.unlink(missing_ok=True)
            configure_mqtt_brokers(ctx)
        else:
            print_info("Keeping existing configuration")

            # Offer owner info update for token-auth brokers
            content = user_toml.read_text()
            if 'method = "token"' in content:
                print()
                print_info("Token-authenticated brokers detected")

                owner_match = re.search(r'owner\s*=\s*"([^"]*)"', content)
                email_match = re.search(r'email\s*=\s*"([^"]*)"', content)
                if owner_match and owner_match.group(1):
                    print_info(f"Current owner: {owner_match.group(1)}")
                if email_match and email_match.group(1):
                    print_info(f"Current email: {email_match.group(1)}")

                # Show remote serial config
                rs_match = re.search(r'\[remote_serial\]\s*\n\s*enabled\s*=\s*(\w+)', content)
                rs_status = rs_match.group(1) if rs_match else "not configured"
                print()
                print(f"  Remote Serial: {rs_status}")

                if prompt_yes_no("Update owner information or remote serial configuration?", "n"):
                    update_owner_info(ctx.config_dir)
    else:
        configure_mqtt_brokers(ctx)

    # ---------------------------------------------------------------------------
    # Permissions and version info
    # ---------------------------------------------------------------------------
    if platform.system() != "Darwin" and ctx.svc_user:
        set_permissions(ctx.install_dir, ctx.config_dir, ctx.svc_user)

    create_version_info(ctx)

    # ---------------------------------------------------------------------------
    # Service restart
    # ---------------------------------------------------------------------------
    print_header("Service Restart")
    print_info(f"Detected existing installation type: {system_type}")

    if system_type == "docker":
        if ctx.update_mode or prompt_yes_no("Update and restart Docker container?", "y"):
            # Copy latest Dockerfile from repo archive (for local build fallback)
            dockerfile_src = os.path.join(repo_dir, "Dockerfile")
            if os.path.exists(dockerfile_src):
                shutil.copy2(dockerfile_src, f"{ctx.install_dir}/Dockerfile")

            # Pull from registry or build locally
            image = pull_or_build_docker_image(ctx)
            if image is None:
                print_error("Failed to obtain Docker image")
            else:
                # Restart container
                ps_result = run_cmd(["docker", "ps", "-a"], check=False, capture=True)
                if ps_result.returncode == 0 and "mctomqtt" in ps_result.stdout:
                    print_info("Restarting container...")
                    run_cmd(["docker", "stop", "mctomqtt"], check=False)
                    run_cmd(["docker", "rm", "mctomqtt"], check=False)

                    # Recreate container
                    serial_device = "/dev/ttyACM0"
                    if user_toml.exists():
                        match = re.search(r'^\s*ports\s*=\s*\["([^"]+)"', user_toml.read_text(), re.MULTILINE)
                        if match:
                            serial_device = match.group(1)

                    parts: list[str] = [
                        "docker", "run", "-d", "--name", "mctomqtt", "--restart", "unless-stopped",
                        "-v", f"{ctx.config_dir}/config.toml:/etc/mctomqtt/config.toml:ro",
                    ]
                    if user_toml.exists():
                        parts.extend(["-v", f"{ctx.config_dir}/config.d/00-user.toml:/etc/mctomqtt/config.d/00-user.toml:ro"])
                    if Path(serial_device).exists():
                        parts.append(f"--device={serial_device}")
                    parts.append(image)

                    result = run_cmd(parts, check=False)
                    if result.returncode == 0:
                        check_service_health("docker")

    elif system_type == "systemd":
        install_systemd_service(
            ctx.install_dir, ctx.config_dir, ctx.svc_user,
            is_update=True, auto=ctx.update_mode,
        )

    elif system_type == "launchd":
        result = run_cmd(["launchctl", "list"], check=False, capture=True)
        if "com.meshcore.mctomqtt" in (result.stdout or ""):
            if ctx.update_mode or prompt_yes_no("Restart launchd service?", "y"):
                run_cmd(["launchctl", "stop", "com.meshcore.mctomqtt"], check=False)
                import time
                time.sleep(2)
                run_cmd(["launchctl", "start", "com.meshcore.mctomqtt"], check=False)
                check_service_health("launchd")
    else:
        print_info("No existing service found")
        if prompt_yes_no("Install service?", "n"):
            from .install_cmd import _install_new_service
            _install_new_service(ctx)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    _print_update_summary(ctx, system_type)


def _print_update_summary(ctx: InstallerContext, system_type: str) -> None:
    """Print update completion summary."""
    print_header("Update Complete!")
    print(f"Installation directory: {ctx.install_dir}")
    print(f"Configuration directory: {ctx.config_dir}")
    print()
    print(f"Base config: {ctx.config_dir}/config.toml")
    print(f"User config: {ctx.config_dir}/config.d/00-user.toml")
    print()

    if system_type == "docker":
        print("Docker container management:")
        print("  Start:   docker start mctomqtt")
        print("  Stop:    docker stop mctomqtt")
        print("  Status:  docker ps -a | grep mctomqtt")
        print("  Logs:    docker logs -f mctomqtt")
        print("  Restart: docker restart mctomqtt")
    elif system_type == "systemd":
        print("Service management:")
        print("  Start:   sudo systemctl start mctomqtt")
        print("  Stop:    sudo systemctl stop mctomqtt")
        print("  Status:  sudo systemctl status mctomqtt")
        print("  Logs:    sudo journalctl -u mctomqtt -f")
    elif system_type == "launchd":
        print("Service management:")
        print("  Start:   sudo launchctl start com.meshcore.mctomqtt")
        print("  Stop:    sudo launchctl stop com.meshcore.mctomqtt")
        print("  Status:  launchctl list | grep mctomqtt")
        print("  Logs:    tail -f /var/log/mctomqtt.log")

    print()
    print_success("Update complete!")
