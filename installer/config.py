"""Configuration: TOML generation, validation, IATA search, and config flows."""

from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, TYPE_CHECKING

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

IATA_API_BASE = "https://api.letsmesh.net/api/iata"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_meshcore_pubkey(key: str) -> str | None:
    """Validate and normalize a MeshCore public key. Returns normalized key or None."""
    key = key.replace(" ", "").upper()
    if len(key) != 64:
        return None
    if not re.fullmatch(r"[0-9A-F]{64}", key):
        return None
    return key


def validate_email(email: str) -> str | None:
    """Validate and normalize an email address. Returns lowercase email or None."""
    if "@" not in email or "." not in email.split("@", 1)[1]:
        return None
    if email.startswith((".", "@")) or email.endswith((".", "@")):
        return None
    if ".." in email or " " in email:
        return None

    local_part, domain = email.split("@", 1)
    if len(local_part) < 1 or len(domain) < 3:
        return None
    if "." not in domain:
        return None

    return email.lower()


# ---------------------------------------------------------------------------
# TOML string escaping
# ---------------------------------------------------------------------------

def toml_escape(val: str) -> str:
    """Escape a string value for use in a TOML quoted string."""
    val = val.replace("\\", "\\\\")
    val = val.replace('"', '\\"')
    return val


def _companions_to_toml_array(csv: str) -> str:
    """Convert comma-separated companion keys to a TOML array string."""
    if not csv:
        return "[]"
    keys = [k.strip() for k in csv.split(",") if k.strip()]
    if not keys:
        return "[]"
    return "[" + ", ".join(f'"{k}"' for k in keys) + "]"


# ---------------------------------------------------------------------------
# TOML generation helpers
# ---------------------------------------------------------------------------

def write_user_toml_base(dest: str, iata: str, serial_device: str, repo: str, branch: str) -> None:
    """Write the initial 00-user.toml with general settings and serial config."""
    content = f"""# MeshCore to MQTT - User Configuration
# This file contains your local overrides to the defaults in config.toml

[general]
iata = "{toml_escape(iata)}"

[serial]
ports = ["{toml_escape(serial_device)}"]

[update]
repo = "{toml_escape(repo)}"
branch = "{toml_escape(branch)}"
"""
    Path(dest).write_text(content)


def append_disabled_broker_toml(dest: str, broker_name: str) -> None:
    """Append a broker block that disables a base-config broker by name."""
    block = f"""
[[broker]]
name = "{toml_escape(broker_name)}"
enabled = false
"""
    with open(dest, "a") as f:
        f.write(block)


def append_letsmesh_broker_toml(
    dest: str,
    broker_name: str,
    server: str,
    audience: str,
    owner: str,
    email: str,
) -> None:
    """Append a LetsMesh broker block to 00-user.toml."""
    block = f"""
[[broker]]
name = "{toml_escape(broker_name)}"
enabled = true
server = "{toml_escape(server)}"
port = 443
transport = "websockets"
keepalive = 60
qos = 0
retain = true

[broker.tls]
enabled = true
verify = true

[broker.auth]
method = "token"
audience = "{toml_escape(audience)}"
owner = "{toml_escape(owner)}"
email = "{toml_escape(email)}"
"""
    with open(dest, "a") as f:
        f.write(block)


def append_custom_broker_toml(
    dest: str,
    broker_name: str,
    server: str,
    port: str,
    transport: str,
    use_tls: str,
    tls_verify: str,
    auth_method: str,
    username: str = "",
    password: str = "",
    audience: str = "",
    owner: str = "",
    email: str = "",
) -> None:
    """Append a custom broker block to 00-user.toml."""
    lines = [
        "",
        "[[broker]]",
        f'name = "{toml_escape(broker_name)}"',
        "enabled = true",
        f'server = "{toml_escape(server)}"',
        f"port = {port}",
        f'transport = "{toml_escape(transport)}"',
        "keepalive = 60",
        "qos = 0",
        "retain = true",
    ]

    if use_tls == "true":
        lines.extend(["", "[broker.tls]", "enabled = true", f"verify = {tls_verify}"])

    lines.extend(["", "[broker.auth]", f'method = "{toml_escape(auth_method)}"'])

    if auth_method == "password":
        lines.append(f'username = "{toml_escape(username)}"')
        lines.append(f'password = "{toml_escape(password)}"')
    elif auth_method == "token":
        if audience:
            lines.append(f'audience = "{toml_escape(audience)}"')
        if owner:
            lines.append(f'owner = "{toml_escape(owner)}"')
        if email:
            lines.append(f'email = "{toml_escape(email)}"')

    lines.append("")
    with open(dest, "a") as f:
        f.write("\n".join(lines))


def append_remote_serial_toml(dest: str, companions_csv: str) -> None:
    """Append remote serial config to 00-user.toml."""
    companions_array = _companions_to_toml_array(companions_csv)
    enabled = "true" if companions_csv else "false"

    block = f"""
[remote_serial]
enabled = {enabled}
allowed_companions = {companions_array}
"""
    with open(dest, "a") as f:
        f.write(block)


# ---------------------------------------------------------------------------
# IATA API helpers (replaces jq dependency)
# ---------------------------------------------------------------------------

def _iata_api_url(params: str, script_version: str = "unknown") -> str:
    return f"{IATA_API_BASE}?{params}&source=installer-{script_version}"


def _iata_request(url: str) -> bytes:
    """Make an HTTP request to the IATA API with a proper User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": "meshcoretomqtt-installer"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def search_iata_api(query: str, script_version: str = "unknown") -> list[tuple[str, str]]:
    """Search IATA airports. Returns list of (code, name) tuples."""
    url = _iata_api_url(f"search={urllib.request.quote(query)}", script_version)
    try:
        data = json.loads(_iata_request(url))
        return [(entry["iata"], entry["name"]) for entry in data]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, OSError):
        return []


def lookup_iata_code(code: str, script_version: str = "unknown") -> str | None:
    """Look up a specific IATA code. Returns airport name or None."""
    url = _iata_api_url(f"code={urllib.request.quote(code)}", script_version)
    try:
        data = json.loads(_iata_request(url))
        return data.get("name")
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, OSError):
        return None


# ---------------------------------------------------------------------------
# Interactive IATA prompts
# ---------------------------------------------------------------------------

def prompt_iata_simple(existing: str = "") -> str:
    """Simple IATA prompt - just asks for 3-letter code."""
    print()
    print_info("IATA code is a 3-letter airport code identifying your region (e.g., SEA, LAX, NYC)")
    print_info("Search/view all IATA codes on a map: https://analyzer.letsmesh.net/map/iata")
    print()

    while True:
        iata = prompt_input("Enter your IATA code (3 letters)", existing).upper().replace(" ", "")

        if not iata or iata == "XXX":
            print_error("Please enter a valid IATA code")
            continue

        if len(iata) != 3:
            print_warning("IATA codes are typically 3 letters")
            if not prompt_yes_no(f"Use '{iata}' anyway?", "n"):
                continue

        return iata


def prompt_iata_letsmesh(existing: str = "", script_version: str = "unknown") -> str:
    """Interactive IATA selection with API search (LetsMesh only)."""
    print()
    print_header("IATA Region Selection")
    print()
    print_info("Your IATA code identifies your geographic region (e.g., SEA, LAX, NYC, LON)")
    print_info("Type to search by airport code or city name")
    print_info("View all IATA codes on a map: https://analyzer.letsmesh.net/map/iata")
    print()

    while True:
        search_query = prompt_input("Search (or enter IATA code directly)")
        if not search_query:
            print_error("Please enter a search term")
            continue

        upper_query = search_query.upper().replace(" ", "")

        # If exactly 3 uppercase letters, try direct lookup
        if re.fullmatch(r"[A-Z]{3}", upper_query):
            print_info(f"Looking up {upper_query}...")
            name = lookup_iata_code(upper_query, script_version)
            if name:
                print()
                print_success(f"Found: {upper_query} - {name}")
                print()
                if prompt_yes_no("Use this IATA code?", "y"):
                    print()
                    print_success(f"Selected: {upper_query} - {name}")
                    return upper_query
                print()
                continue
            else:
                print_error(f"IATA code '{upper_query}' not found in database")
                print()
                continue

        # Search via API
        print_info("Searching...")
        results = search_iata_api(search_query, script_version)

        if not results:
            print_error(f"No matching airports found for '{search_query}'")
            print()
            continue

        # Display results
        print()
        print_info("Matching airports:")
        print()
        for i, (iata, name) in enumerate(results, 1):
            print(f"  {i}) {iata} - {name}")
        print()
        print("  s) Search again")
        print()

        choice = prompt_input(f"Select [1-{len(results)}] or 's' to search again")

        if choice.lower() == "s":
            print()
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(results):
            idx = int(choice) - 1
            selected_iata, selected_name = results[idx]
            print()
            print_success(f"Selected: {selected_iata} - {selected_name}")
            return selected_iata

        print_error("Invalid selection")
        print()


# ---------------------------------------------------------------------------
# Interactive prompts for owner info and companions
# ---------------------------------------------------------------------------

def prompt_owner_email(existing: str = "") -> str:
    """Prompt for owner email with validation. Returns email or empty string."""
    print()
    print_info("Owner email")
    print()

    while True:
        email = prompt_input("Enter owner email (or leave empty to skip)", existing)

        if not email:
            return ""

        validated = validate_email(email)
        if validated is not None:
            return validated

        print_error("Invalid email format")
        if not prompt_yes_no("Try again?", "y"):
            return ""


def prompt_owner_pubkey(existing: str = "") -> str:
    """Prompt for owner public key with validation. Returns key or empty string."""
    print()
    print_info("Owner public key is a 64-character hex string (MeshCore companion public key)")
    print_info("Example: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    print()

    while True:
        owner = prompt_input("Enter owner public key (or leave empty to skip)", existing)

        if not owner:
            return ""

        validated = validate_meshcore_pubkey(owner)
        if validated is not None:
            return validated

        print_error("Invalid public key format. Must be 64 hex characters (32 bytes)")
        if not prompt_yes_no("Try again?", "y"):
            return ""


def prompt_allowed_companions(existing: str = "") -> str:
    """Prompt for remote serial allowed companions. Returns comma-separated keys or empty."""
    print()
    print_header("Remote Serial Access (Experimental)")
    print()
    print_info("Remote Serial allows you to execute serial commands on your node")
    print_info("remotely via the LetsMesh Packet Analyzer web interface.")
    print()
    print_info("You must specify which companion devices (by public key) are")
    print_info("authorized to send commands. Commands are cryptographically signed.")
    print()

    if existing:
        print_info("Current allowed companions:")
        for key in existing.split(","):
            key = key.strip()
            if key:
                print(f"  - {key}")
        print()

    if not prompt_yes_no("Configure remote serial access?", "n"):
        return existing

    print()
    print_info("Enter companion public keys (64 hex chars each)")
    print_info("Enter one key at a time. Leave empty when done.")
    print()

    keys: list[str] = []
    key_num = 1

    while True:
        key = prompt_input(f"Companion {key_num} public key (empty to finish)")

        if not key:
            break

        validated = validate_meshcore_pubkey(key)
        if validated is not None:
            keys.append(validated)
            print_success(f"Added: {validated}")
            key_num += 1
        else:
            print_error("Invalid public key format. Must be 64 hex characters.")

    if not keys:
        return ""

    return ",".join(keys)


# ---------------------------------------------------------------------------
# Configure custom broker (interactive)
# ---------------------------------------------------------------------------

def configure_custom_broker(broker_num: int, config_dir: str, decoder_available: bool) -> None:
    """Configure a single custom MQTT broker interactively."""
    user_toml = f"{config_dir}/config.d/00-user.toml"

    print()
    print_header(f"Configuring MQTT Broker {broker_num}")

    server = prompt_input("Server hostname/IP")
    if not server:
        print_warning(f"Server hostname required - skipping broker {broker_num}")
        return

    port = prompt_input("Port", "1883")
    transport = "websockets" if prompt_yes_no("Use WebSockets transport?", "n") else "tcp"

    use_tls = "false"
    tls_verify = "true"
    if prompt_yes_no("Use TLS/SSL encryption?", "n"):
        use_tls = "true"
        if not prompt_yes_no("Verify TLS certificates?", "y"):
            tls_verify = "false"

    print()
    print_info("Authentication method:")
    print("  1) Username/Password")
    print("  2) MeshCore Auth Token (requires meshcore-decoder)")
    print("  3) None (anonymous)")
    auth_choice = prompt_input("Choose authentication method [1-3]", "1")

    auth_method = "none"
    username = password = audience = owner = email = ""

    if auth_choice == "2":
        if not decoder_available:
            print_error("meshcore-decoder not available - using username/password instead")
            auth_choice = "1"
        else:
            auth_method = "token"
            audience = prompt_input("Token audience (optional)")
            owner = prompt_owner_pubkey()
            email = prompt_owner_email()

            parts = []
            if owner and email:
                parts.append(f"Owner info set: {owner} ({email})")
            elif owner:
                parts.append(f"Owner public key set: {owner}")
            elif email:
                parts.append(f"Owner email set: {email}")
            if parts:
                print_success(parts[0])

    if auth_choice == "1":
        auth_method = "password"
        username = prompt_input("Username")
        if username:
            password = prompt_input("Password")

    broker_name = f"custom-{broker_num}"
    append_custom_broker_toml(
        user_toml, broker_name, server, port, transport,
        use_tls, tls_verify, auth_method,
        username, password, audience, owner, email,
    )
    print_success(f"Broker {broker_num} configured")


# ---------------------------------------------------------------------------
# Configure MQTT brokers (main flow)
# ---------------------------------------------------------------------------

def configure_mqtt_brokers(ctx: InstallerContext) -> None:
    """Interactive MQTT broker configuration flow."""
    user_toml = f"{ctx.config_dir}/config.d/00-user.toml"

    # Ensure 00-user.toml exists with base settings
    if not Path(user_toml).exists():
        from .system import select_serial_device
        serial_device = select_serial_device()
        write_user_toml_base(user_toml, "XXX", serial_device, ctx.repo, ctx.branch)

    print()
    print_header("MQTT Broker Configuration")
    print()
    print_info("Enable the LetsMesh.net Packet Analyzer MQTT servers?")
    print("  - Real-time packet analysis and visualization")
    print("  - Network health monitoring")
    print("  - Includes US and EU regional brokers for redundancy")
    print("  - Requires meshcore-decoder for authentication")
    print()

    if ctx.decoder_available:
        if prompt_yes_no("Enable LetsMesh Packet Analyzer MQTT servers?", "y"):
            # LetsMesh IATA selection via API (no jq needed!)
            existing_iata = _read_existing_iata(user_toml)
            if not existing_iata or existing_iata == "XXX":
                iata = prompt_iata_letsmesh("", ctx.script_version)
                _update_iata_in_file(user_toml, iata)
                print_success(f"IATA code set to: {iata}")

            # Prompt for owner info
            print()
            print_info("LetsMesh Packet Analyzer supports optional owner identification")
            print_info("This links your observer to your MeshCore public key and email")
            owner_pubkey = prompt_owner_pubkey()
            owner_email = prompt_owner_email()
            allowed_companions = prompt_allowed_companions()

            # Configure US and EU brokers
            append_letsmesh_broker_toml(
                user_toml, "letsmesh-us",
                "mqtt-us-v1.letsmesh.net", "mqtt-us-v1.letsmesh.net",
                owner_pubkey, owner_email,
            )
            append_letsmesh_broker_toml(
                user_toml, "letsmesh-eu",
                "mqtt-eu-v1.letsmesh.net", "mqtt-eu-v1.letsmesh.net",
                owner_pubkey, owner_email,
            )

            if allowed_companions:
                append_remote_serial_toml(user_toml, allowed_companions)
                count = len([k for k in allowed_companions.split(",") if k.strip()])
                print_success(f"Remote serial access enabled with {count} companion(s)")

            # Build success message
            owner_info = ""
            if owner_pubkey and owner_email:
                owner_info = f" with owner: {owner_pubkey} ({owner_email})"
            elif owner_pubkey:
                owner_info = f" with owner: {owner_pubkey}"
            elif owner_email:
                owner_info = f" with email: {owner_email}"
            print_success(f"LetsMesh Packet Analyzer MQTT servers enabled{owner_info}")

            if prompt_yes_no("Would you like to configure additional MQTT brokers?", "n"):
                _configure_additional_brokers(ctx)
        else:
            # User declined LetsMesh — disable both default brokers
            append_disabled_broker_toml(user_toml, "letsmesh-us")
            append_disabled_broker_toml(user_toml, "letsmesh-eu")
            if prompt_yes_no("Would you like to configure a custom MQTT broker?", "y"):
                _configure_iata_simple(user_toml)
                configure_custom_broker(1, ctx.config_dir, ctx.decoder_available)
                if prompt_yes_no("Would you like to configure additional MQTT brokers?", "n"):
                    _configure_additional_brokers(ctx)
            else:
                print_warning(f"No MQTT brokers configured - you'll need to edit {user_toml} manually")
    else:
        # No decoder available — LetsMesh brokers can't work, disable them
        append_disabled_broker_toml(user_toml, "letsmesh-us")
        append_disabled_broker_toml(user_toml, "letsmesh-eu")
        print_warning("meshcore-decoder not available - cannot use LetsMesh auth token authentication")
        if prompt_yes_no("Would you like to configure a custom MQTT broker with username/password?", "y"):
            _configure_iata_simple(user_toml)
            configure_custom_broker(1, ctx.config_dir, ctx.decoder_available)
            if prompt_yes_no("Would you like to configure additional MQTT brokers?", "n"):
                _configure_additional_brokers(ctx)
        else:
            print_warning(f"No MQTT brokers configured - you'll need to edit {user_toml} manually")

    # Fix ownership after writing config
    if platform.system() != "Darwin" and ctx.svc_user:
        import shutil as _shutil
        _shutil.chown(user_toml, "root", ctx.svc_user)
        os.chmod(user_toml, 0o640)


def _configure_iata_simple(user_toml: str) -> None:
    """Prompt for simple IATA and update 00-user.toml."""
    existing_iata = _read_existing_iata(user_toml)
    if not existing_iata or existing_iata == "XXX":
        iata = prompt_iata_simple()
        _update_iata_in_file(user_toml, iata)
        print_success(f"IATA code set to: {iata}")


def _configure_additional_brokers(ctx: InstallerContext) -> None:
    """Configure additional custom brokers."""
    user_toml = f"{ctx.config_dir}/config.d/00-user.toml"
    # Count existing broker blocks
    existing_count = 0
    if Path(user_toml).exists():
        content = Path(user_toml).read_text()
        existing_count = content.count("[[broker]]")

    num_str = prompt_input("How many additional brokers?", "1")
    try:
        num_additional = int(num_str)
    except ValueError:
        num_additional = 1

    for i in range(num_additional):
        broker_num = existing_count + i + 1
        configure_custom_broker(broker_num, ctx.config_dir, ctx.decoder_available)


# ---------------------------------------------------------------------------
# Update owner info for existing config
# ---------------------------------------------------------------------------

def update_owner_info(config_dir: str) -> None:
    """Update owner public key and email for existing token-auth brokers."""
    user_toml = f"{config_dir}/config.d/00-user.toml"

    if not Path(user_toml).exists():
        print_error("No configuration file found")
        return

    print()
    print_header("Update Owner Information")

    content = Path(user_toml).read_text()
    if 'method = "token"' not in content:
        print_warning("No brokers configured with auth token authentication")
        return

    print_info("This will update owner and email for all token-auth brokers")
    print()

    # Extract existing owner and email
    existing_owner = ""
    existing_email = ""
    owner_match = re.search(r'^owner\s*=\s*"([^"]*)"', content, re.MULTILINE)
    if owner_match:
        existing_owner = owner_match.group(1)
    email_match = re.search(r'^email\s*=\s*"([^"]*)"', content, re.MULTILINE)
    if email_match:
        existing_email = email_match.group(1)

    if existing_owner:
        print_info(f"Current owner: {existing_owner}")
    if existing_email:
        print_info(f"Current email: {existing_email}")

    new_owner = prompt_owner_pubkey(existing_owner)
    new_email = prompt_owner_email(existing_email)

    # Back up config
    import shutil
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(user_toml, f"{user_toml}.backup-{timestamp}")

    # Update owner and email
    if new_owner:
        content = re.sub(r'^(owner\s*=\s*).*$', f'\\1"{new_owner}"', content, flags=re.MULTILINE)
    if new_email:
        content = re.sub(r'^(email\s*=\s*).*$', f'\\1"{new_email}"', content, flags=re.MULTILINE)
    Path(user_toml).write_text(content)

    # Prompt for remote serial companions
    existing_companions = ""
    comp_match = re.search(r'allowed_companions\s*=\s*\[(.*?)\]', content)
    if comp_match:
        raw = comp_match.group(1)
        existing_companions = ",".join(
            k.strip().strip('"') for k in raw.split(",") if k.strip().strip('"')
        )

    new_companions = prompt_allowed_companions(existing_companions)

    if new_companions != existing_companions:
        companions_array = _companions_to_toml_array(new_companions)
        new_enabled = "true" if new_companions else "false"

        content = Path(user_toml).read_text()
        if "[remote_serial]" in content:
            content = re.sub(
                r'^(enabled\s*=\s*).*$', f"\\1{new_enabled}",
                content, count=1, flags=re.MULTILINE,
            )
            content = re.sub(
                r'^(allowed_companions\s*=\s*).*$', f"\\1{companions_array}",
                content, count=1, flags=re.MULTILINE,
            )
            Path(user_toml).write_text(content)
        else:
            append_remote_serial_toml(user_toml, new_companions)

    # Summary
    changes = []
    if new_owner:
        changes.append(f"owner: {new_owner}")
    if new_email:
        changes.append(f"email: {new_email}")
    if new_companions != existing_companions:
        if new_companions:
            count = len([k for k in new_companions.split(",") if k.strip()])
            changes.append(f"remote serial: enabled with {count} companion(s)")
        else:
            changes.append("remote serial: disabled")

    if changes:
        print_success(f"Updated configuration: {', '.join(changes)}")
    else:
        print_success("No changes made")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_existing_iata(user_toml: str) -> str:
    """Read the existing IATA code from 00-user.toml."""
    if not Path(user_toml).exists():
        return ""
    content = Path(user_toml).read_text()
    match = re.search(r'^\s*iata\s*=\s*"([^"]*)"', content, re.MULTILINE)
    return match.group(1) if match else ""


def _update_iata_in_file(user_toml: str, iata: str) -> None:
    """Update the iata value in 00-user.toml."""
    content = Path(user_toml).read_text()
    content = re.sub(r'^(iata\s*=\s*).*$', f'\\1"{iata}"', content, flags=re.MULTILINE)
    Path(user_toml).write_text(content)


# Need platform for the import in configure_mqtt_brokers
import platform
