"""Fresh install orchestration for MeshCore to MQTT."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .config import configure_mqtt_brokers, update_owner_info, _read_existing_iata, prompt_iata_letsmesh, prompt_iata_simple, _update_iata_in_file
from .migrate_cmd import detect_old_installation, run_migrate
from .system import (
    create_system_user,
    create_venv,
    create_version_info,
    detect_system_type,
    detect_system_type_native,
    docker_cmd,
    download_file,
    download_repo_archive,
    install_docker_service,
    install_launchd_service,
    install_meshcore_decoder,
    install_systemd_service,
    prompt_service_user,
    run_cmd,
    set_permissions,
)
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


def run_install(ctx: InstallerContext) -> None:
    """Run a fresh installation."""
    tmp_dir = tempfile.mkdtemp()
    try:
        _do_install(ctx, tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _do_install(ctx: InstallerContext, tmp_dir: str) -> None:
    # Download repo archive (or use local install path)
    if ctx.local_install:
        repo_dir = ctx.local_install
    else:
        try:
            repo_dir = download_repo_archive(ctx.repo, ctx.branch, tmp_dir)
        except subprocess.CalledProcessError:
            print_error("Failed to download repository archive")
            raise SystemExit(1)

    ctx.repo_dir = repo_dir

    # Copy mctomqtt.py to extract version
    mctomqtt_tmp = os.path.join(tmp_dir, "mctomqtt.py")
    shutil.copy2(os.path.join(repo_dir, "mctomqtt.py"), mctomqtt_tmp)

    # Extract version
    for line in Path(mctomqtt_tmp).read_text().splitlines():
        if line.startswith("__version__"):
            match = re.search(r'"([^"]+)"', line)
            if match:
                ctx.script_version = match.group(1)
            break

    print_header(f"MeshCore to MQTT Installer v{ctx.script_version}")
    print()
    print("This installer will help you set up MeshCore to MQTT relay.")
    print()

    # ---------------------------------------------------------------------------
    # Check for legacy installation and migrate
    # ---------------------------------------------------------------------------
    migration_done = run_migrate(ctx)

    # Determine directories
    print_info(f"Installation directory: {ctx.install_dir}")
    print_info(f"Configuration directory: {ctx.config_dir}")

    # Check if functional installation exists
    updating_existing = False
    user_toml = Path(ctx.config_dir) / "config.d" / "00-user.toml"
    has_existing = (
        Path(ctx.install_dir, "mctomqtt.py").exists()
        and user_toml.exists()
        and "[[broker]]" in (user_toml.read_text() if user_toml.exists() else "")
    )

    if has_existing:
        if ctx.update_mode:
            print_info("Update mode - updating existing installation...")
            updating_existing = True
        elif migration_done:
            # Just migrated, treat as new install that needs service setup
            updating_existing = False
        elif prompt_yes_no("Existing installation found. Update?", "y"):
            print_info("Updating existing installation...")
            updating_existing = True
        else:
            print_error("Installation cancelled.")
            raise SystemExit(1)

    # If we're updating, delegate to the update command
    if updating_existing:
        from .update_cmd import run_update
        run_update(ctx)
        return

    # ---------------------------------------------------------------------------
    # Service account (Linux only)
    # ---------------------------------------------------------------------------
    if platform.system() != "Darwin":
        print()
        print_header("Service Account")
        ctx.svc_user = prompt_service_user(ctx)
        create_system_user(ctx.svc_user, ctx.install_dir)
    else:
        ctx.svc_user = ""

    # Create directories
    os.makedirs(ctx.install_dir, exist_ok=True)
    os.makedirs(f"{ctx.config_dir}/config.d", exist_ok=True)

    # ---------------------------------------------------------------------------
    # Installation method
    # ---------------------------------------------------------------------------
    print_header("Installation Method")
    print()
    print_info("Choose installation method:")
    print("  1) System service (systemd/launchd) - installs Python dependencies on host")
    print("  2) Docker container - all dependencies in container (requires docker to be installed)")
    print("  3) Manual run only (install files, no auto-start)")
    print()
    ctx.install_method = prompt_input("Choose installation method [1-3]", "1")

    # Write install type marker
    install_type_map = {
        "1": detect_system_type_native(),
        "2": "docker",
        "3": "manual",
    }
    install_type = install_type_map.get(ctx.install_method, "manual")
    Path(f"{ctx.install_dir}/.install_type").write_text(install_type)

    # ---------------------------------------------------------------------------
    # Dependencies
    # ---------------------------------------------------------------------------
    if ctx.install_method == "2":
        # Docker includes meshcore-decoder
        ctx.decoder_available = True
    else:
        print_header("Checking Dependencies")

        # Check Python 3.11+
        _check_python_version()

        # Set up venv
        print_info("Setting up Python virtual environment...")
        create_venv(ctx.install_dir, ctx.svc_user)

        # Check for meshcore-decoder
        ctx.decoder_available = False
        if shutil.which("meshcore-decoder"):
            print_success(f"meshcore-decoder found: {shutil.which('meshcore-decoder')}")
            ctx.decoder_available = True
        elif prompt_yes_no("Install meshcore-decoder for LetsMesh auth support?", "y"):
            ctx.decoder_available = install_meshcore_decoder(ctx.install_dir, ctx.svc_user)

    # ---------------------------------------------------------------------------
    # Install files from repo
    # ---------------------------------------------------------------------------
    print_header("Installing Files")

    if ctx.local_install:
        print_info(f"Installing from local directory: {repo_dir}")
    else:
        print_info(f"Installing from GitHub ({ctx.repo} @ {ctx.branch})...")

    shutil.copy2(os.path.join(repo_dir, "auth_token.py"), os.path.join(tmp_dir, "auth_token.py"))
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

    # Install to target directories
    shutil.copy2(mctomqtt_tmp, f"{ctx.install_dir}/")
    shutil.copy2(os.path.join(tmp_dir, "auth_token.py"), f"{ctx.install_dir}/")
    shutil.copy2(os.path.join(tmp_dir, "uninstall.sh"), f"{ctx.install_dir}/")
    for f in ("mctomqtt.service", "com.meshcore.mctomqtt.plist"):
        src = os.path.join(tmp_dir, f)
        if os.path.exists(src):
            shutil.copy2(src, f"{ctx.install_dir}/")
    os.chmod(f"{ctx.install_dir}/mctomqtt.py", 0o755)
    os.chmod(f"{ctx.install_dir}/uninstall.sh", 0o755)

    # Install base config
    shutil.copy2(os.path.join(tmp_dir, "config.toml.example"), f"{ctx.config_dir}/config.toml")
    print_success(f"Base config installed to {ctx.config_dir}/config.toml")
    print_success(f"Files installed to {ctx.install_dir}")

    # ---------------------------------------------------------------------------
    # Configuration
    # ---------------------------------------------------------------------------
    print_header("Configuration")

    if ctx.config_url:
        _handle_config_url(ctx, user_toml)
    elif migration_done and user_toml.exists():
        print_success("Using migrated configuration")
        if "[[broker]]" not in user_toml.read_text():
            print_warning("No MQTT brokers found in migrated config")
            configure_mqtt_brokers(ctx)
    elif not user_toml.exists():
        configure_mqtt_brokers(ctx)
    elif "[[broker]]" not in user_toml.read_text():
        print_warning("Incomplete configuration detected - MQTT brokers not configured")
        configure_mqtt_brokers(ctx)

    # ---------------------------------------------------------------------------
    # Permissions and version info
    # ---------------------------------------------------------------------------
    if platform.system() != "Darwin" and ctx.svc_user:
        set_permissions(ctx.install_dir, ctx.config_dir, ctx.svc_user)

    create_version_info(ctx)

    # ---------------------------------------------------------------------------
    # Service installation
    # ---------------------------------------------------------------------------
    _install_new_service(ctx)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    _print_install_summary(ctx, migration_done)


def _install_new_service(ctx: InstallerContext) -> None:
    """Install service based on chosen method."""
    print_header("Service Installation")

    docker_installed = False
    service_installed = False
    system_type = ""

    if ctx.install_method == "1":
        system_type = detect_system_type_native()
        print_info(f"Detected system type: {system_type}")
        if system_type == "systemd":
            service_installed = install_systemd_service(
                ctx.install_dir, ctx.config_dir, ctx.svc_user,
            )
        elif system_type == "launchd":
            service_installed = install_launchd_service(
                ctx.install_dir, ctx.config_dir,
            )
        else:
            print_error(f"Unsupported system type: {system_type}")
            print_info("You'll need to manually configure the service")
    elif ctx.install_method == "2":
        docker_installed = install_docker_service(ctx)
    elif ctx.install_method == "3":
        print_info("Skipping service installation")
        print_info(f"To run manually: {ctx.install_dir}/venv/bin/python3 {ctx.install_dir}/mctomqtt.py --config {ctx.config_dir}/config.toml")
    else:
        print_warning("Invalid selection, skipping service installation")
        print_info(f"To run manually: {ctx.install_dir}/venv/bin/python3 {ctx.install_dir}/mctomqtt.py --config {ctx.config_dir}/config.toml")

    # Store for summary
    ctx._docker_installed = docker_installed
    ctx._service_installed = service_installed
    ctx._system_type = system_type


def _print_install_summary(ctx: InstallerContext, migration_done: bool) -> None:
    """Print installation completion summary."""
    print_header("Installation Complete!")
    print(f"Installation directory: {ctx.install_dir}")
    print(f"Configuration directory: {ctx.config_dir}")
    print()
    print(f"Base config: {ctx.config_dir}/config.toml")
    print(f"User config: {ctx.config_dir}/config.d/00-user.toml")
    print()

    docker_installed = getattr(ctx, "_docker_installed", False)
    service_installed = getattr(ctx, "_service_installed", False)
    system_type = getattr(ctx, "_system_type", "")

    if docker_installed:
        print("Docker container management:")
        print("  Start:   docker start mctomqtt")
        print("  Stop:    docker stop mctomqtt")
        print("  Status:  docker ps -a | grep mctomqtt")
        print("  Logs:    docker logs -f mctomqtt")
        print("  Restart: docker restart mctomqtt")
    elif service_installed:
        if system_type == "systemd":
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
    else:
        print(f"Manual run: {ctx.install_dir}/venv/bin/python3 {ctx.install_dir}/mctomqtt.py --config {ctx.config_dir}/config.toml")

    if migration_done:
        print()
        home = Path.home()
        print_info(f"Migration note: old installation preserved at {home}/.meshcoretomqtt")
        print_info(f"Remove it once you have verified the new installation: rm -rf {home}/.meshcoretomqtt")

    print()
    print_success("Installation complete!")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_python_version() -> None:
    """Verify Python 3.11+ is available."""
    import sys

    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 11):
        print_error(f"Python 3.11 or later is required (found Python {major}.{minor})")
        print_info("Please upgrade Python and try again.")
        raise SystemExit(1)

    print_success(f"Python {major}.{minor} found (>= 3.11 required)")


def _handle_config_url(ctx: InstallerContext, user_toml: Path) -> None:
    """Handle --config URL: download and set up 00-user.toml from a URL."""
    print_info(f"Downloading configuration from: {ctx.config_url}")

    try:
        download_file(ctx.config_url, str(user_toml), "00-user.toml")
    except subprocess.CalledProcessError:
        print_error("Failed to download configuration from URL")
        if prompt_yes_no("Continue with interactive configuration?", "y"):
            configure_mqtt_brokers(ctx)
        else:
            raise SystemExit(1)
        return

    print_success("Configuration downloaded successfully")

    # Show what was downloaded
    print()
    print_info("Downloaded configuration:")
    content = user_toml.read_text()
    non_empty = [l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    for line in non_empty[:20]:
        print(line)
    if len(non_empty) > 20:
        print("...")
    print()

    if prompt_yes_no("Use this configuration?", "y"):
        print_success("Using downloaded configuration")

        # Prompt for IATA
        existing_iata = _read_existing_iata(str(user_toml))
        has_letsmesh = "letsmesh" in content

        if has_letsmesh:
            iata = prompt_iata_letsmesh(existing_iata, ctx.script_version)
        else:
            iata = prompt_iata_simple(existing_iata)

        if "iata = " in content:
            _update_iata_in_file(str(user_toml), iata)
        else:
            # Prepend general section with iata
            new_content = f'[general]\niata = "{iata}"\n\n' + content
            user_toml.write_text(new_content)

        print_success(f"IATA code set to: {iata}")

        if "[[broker]]" in content:
            print_success("MQTT brokers already configured in downloaded config")
            if prompt_yes_no("Would you like to configure additional MQTT brokers?", "n"):
                from .config import _configure_additional_brokers
                _configure_additional_brokers(ctx)
        else:
            configure_mqtt_brokers(ctx)
    else:
        user_toml.unlink(missing_ok=True)
        configure_mqtt_brokers(ctx)
