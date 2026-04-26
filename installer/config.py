"""Configuration: TOML generation, validation, IATA search, and config flows."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse
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
USER_CONFIG_FILENAME = "99-user.toml"
LEGACY_USER_CONFIG_FILENAME = "00-user.toml"
PRESET_PREFIX = "10-"


def user_config_path(config_dir: str | Path) -> Path:
    """Return the canonical user override path."""
    return Path(config_dir) / "config.d" / USER_CONFIG_FILENAME


def legacy_user_config_path(config_dir: str | Path) -> Path:
    """Return the legacy user override path."""
    return Path(config_dir) / "config.d" / LEGACY_USER_CONFIG_FILENAME


def migrate_user_config_filename(config_dir: str | Path) -> Path:
    """Rename legacy 00-user.toml to 99-user.toml so user overrides load last."""
    new_path = user_config_path(config_dir)
    old_path = legacy_user_config_path(config_dir)

    if old_path.exists() and new_path.exists():
        print_error(
            f"Both {old_path} and {new_path} exist. Remove or merge one before continuing."
        )
        raise SystemExit(1)

    if old_path.exists():
        old_path.rename(new_path)
        print_success(f"Migrated user config to {new_path}")

    return new_path


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
    """Write the initial user TOML with general settings and serial config."""
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
    """Append a LetsMesh broker block to a TOML file."""
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
    """Append a custom broker block to a TOML file."""
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
    """Append remote serial config to a TOML file."""
    companions_array = _companions_to_toml_array(companions_csv)
    enabled = "true" if companions_csv else "false"

    block = f"""
[remote_serial]
enabled = {enabled}
allowed_companions = {companions_array}
"""
    with open(dest, "a") as f:
        f.write(block)


def append_token_owner_overrides_toml(
    dest: str,
    broker_names: list[str],
    owner: str,
    email: str,
) -> None:
    """Append local auth metadata overrides for token-auth preset brokers."""
    if not owner and not email:
        return

    lines: list[str] = []
    for broker_name in broker_names:
        lines.extend([
            "",
            "[[broker]]",
            f'name = "{toml_escape(broker_name)}"',
            "",
            "[broker.auth]",
        ])
        if owner:
            lines.append(f'owner = "{toml_escape(owner)}"')
        if email:
            lines.append(f'email = "{toml_escape(email)}"')

    lines.append("")
    with open(dest, "a") as f:
        f.write("\n".join(lines))


def _rewrite_token_owner_overrides_toml(
    dest: str,
    broker_names: list[str],
    owner: str,
    email: str,
) -> None:
    """Replace local auth metadata overrides for the given brokers."""
    path = Path(dest)
    data: dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    brokers = data.get("broker", [])
    if not isinstance(brokers, list):
        brokers = []

    by_name: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for broker in brokers:
        if not isinstance(broker, dict):
            continue
        name = broker.get("name")
        if isinstance(name, str):
            by_name[name] = broker
        ordered.append(broker)

    for broker_name in broker_names:
        broker = by_name.get(broker_name)
        if broker is None:
            broker = {"name": broker_name}
            by_name[broker_name] = broker
            ordered.append(broker)
        auth = broker.get("auth")
        if not isinstance(auth, dict):
            auth = {}
            broker["auth"] = auth
        if owner:
            auth["owner"] = owner
        else:
            auth.pop("owner", None)
        if email:
            auth["email"] = email
        else:
            auth.pop("email", None)

    content = _toml_dumps_user_config(data, ordered)
    path.write_text(content)


def _remove_broker_overrides_toml(dest: str | Path, broker_names: list[str]) -> None:
    """Remove local broker override blocks for the given broker names."""
    path = Path(dest)
    if not path.exists():
        return

    with open(path, "rb") as f:
        data = tomllib.load(f)

    brokers = data.get("broker", [])
    if not isinstance(brokers, list):
        return

    remove_names = set(broker_names)
    kept = [
        broker for broker in brokers
        if not isinstance(broker, dict) or broker.get("name") not in remove_names
    ]
    content = _toml_dumps_user_config(data, kept)
    path.write_text(content)


def _toml_dumps_user_config(data: dict[str, Any], brokers: list[dict[str, Any]]) -> str:
    """Serialize installer-managed user config TOML."""
    lines = [
        "# MeshCore to MQTT - User Configuration",
        "# This file contains your local overrides to the defaults in config.toml",
        "",
    ]

    for section in ("general", "serial", "update", "remote_serial"):
        value = data.get(section)
        if isinstance(value, dict):
            lines.append(f"[{section}]")
            for key, item in value.items():
                lines.append(f"{key} = {_toml_value(item)}")
            lines.append("")

    for broker in brokers:
        name = broker.get("name")
        if not isinstance(name, str):
            continue
        lines.append("[[broker]]")
        lines.append(f'name = "{toml_escape(name)}"')
        for key, item in broker.items():
            if key in ("name", "auth", "tls", "topics") or isinstance(item, dict):
                continue
            lines.append(f"{key} = {_toml_value(item)}")
        for nested in ("tls", "auth", "topics"):
            nested_data = broker.get(nested)
            if isinstance(nested_data, dict):
                lines.append("")
                lines.append(f"[broker.{nested}]")
                for key, item in nested_data.items():
                    lines.append(f"{key} = {_toml_value(item)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return f'"{toml_escape(str(value))}"'


# ---------------------------------------------------------------------------
# Preset helpers
# ---------------------------------------------------------------------------

def _safe_preset_basename(name: str) -> str:
    """Validate and return a safe TOML preset basename."""
    basename = Path(name).name
    if basename != name or not basename or basename in (".", ".."):
        raise ValueError("Preset filename must be a simple basename")
    if "/" in basename or "\\" in basename:
        raise ValueError("Preset filename must not contain path separators")
    if not basename.endswith(".toml"):
        raise ValueError("Preset filename must end with .toml")
    if basename.startswith("."):
        raise ValueError("Preset filename must not be hidden")
    return basename


def preset_dest_path(config_dir: str | Path, original_filename: str) -> Path:
    """Return the active config.d path for a preset filename."""
    basename = _safe_preset_basename(original_filename)
    if basename.startswith(PRESET_PREFIX):
        dest_name = basename
    else:
        dest_name = f"{PRESET_PREFIX}{basename}"
    return Path(config_dir) / "config.d" / dest_name


def validate_preset_toml(path: str | Path) -> dict[str, Any]:
    """Load and validate a broker preset TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    brokers = data.get("broker")
    if not isinstance(brokers, list) or not brokers:
        raise ValueError("Preset must contain at least one [[broker]] block")

    for broker in brokers:
        if not isinstance(broker, dict) or not broker.get("name"):
            raise ValueError("Every preset broker must have a name")

    return data


def list_bundled_presets(repo_dir: str | Path) -> list[Path]:
    """List bundled preset TOML files from the checked-out or downloaded repo."""
    preset_dir = Path(repo_dir) / "presets"
    if not preset_dir.is_dir():
        return []
    return sorted(p for p in preset_dir.glob("*.toml") if p.is_file())


def copy_preset_to_config(source: str | Path, config_dir: str | Path) -> Path:
    """Validate and copy a preset into config.d with the preset prefix."""
    source_path = Path(source)
    dest = preset_dest_path(config_dir, source_path.name)
    validate_preset_toml(source_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest)
    return dest


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    return Path(parsed.path).name


def import_preset_to_config(source: str, config_dir: str | Path) -> Path:
    """Import a preset from a local path or URL into config.d."""
    if re.match(r"^https?://", source):
        filename = _safe_preset_basename(_filename_from_url(source))
        dest = preset_dest_path(config_dir, filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            req = urllib.request.Request(
                source,
                headers={"User-Agent": "meshcoretomqtt-installer"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                tmp_path.write_bytes(resp.read())
            validate_preset_toml(tmp_path)
            shutil.copy2(tmp_path, dest)
        finally:
            tmp_path.unlink(missing_ok=True)
        return dest

    source_path = Path(source).expanduser()
    _safe_preset_basename(source_path.name)
    return copy_preset_to_config(source_path, config_dir)


def token_broker_names_from_preset(path: str | Path) -> list[tuple[str, str]]:
    """Return (preset filename, broker name) entries for token-auth brokers."""
    data = validate_preset_toml(path)
    names: list[tuple[str, str]] = []
    for broker in data.get("broker", []):
        auth = broker.get("auth", {})
        if isinstance(auth, dict) and auth.get("method") == "token":
            names.append((Path(path).name, str(broker["name"])))
    return names


def token_preset_brokers(config_dir: str | Path) -> dict[Path, list[str]]:
    """Return token-auth broker names grouped by active preset file."""
    config_d = Path(config_dir) / "config.d"
    result: dict[Path, list[str]] = {}
    if not config_d.is_dir():
        return result

    for preset in sorted(config_d.glob(f"{PRESET_PREFIX}*.toml")):
        try:
            broker_names = [name for _preset, name in token_broker_names_from_preset(preset)]
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            continue
        if broker_names:
            result[preset] = broker_names
    return result


def configured_presets(config_dir: str | Path) -> dict[Path, list[str]]:
    """Return active preset files and their broker names."""
    config_d = Path(config_dir) / "config.d"
    result: dict[Path, list[str]] = {}
    if not config_d.is_dir():
        return result

    for preset in sorted(config_d.glob(f"{PRESET_PREFIX}*.toml")):
        try:
            data = validate_preset_toml(preset)
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            result[preset] = []
            continue
        brokers = data.get("broker", [])
        names = [
            str(broker["name"]) for broker in brokers
            if isinstance(broker, dict) and broker.get("name")
        ]
        result[preset] = names
    return result


def _broker_auth_metadata(config_dir: str | Path, broker_name: str) -> tuple[str, str]:
    """Return effective owner/email for a broker from config.d load order."""
    owner = ""
    email = ""
    config_d = Path(config_dir) / "config.d"
    if not config_d.is_dir():
        return owner, email

    for path in sorted(config_d.glob("*.toml")):
        try:
            data = validate_preset_toml(path) if path.name.startswith(PRESET_PREFIX) else _load_toml_file(path)
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            continue
        brokers = data.get("broker", [])
        if not isinstance(brokers, list):
            continue
        for broker in brokers:
            if not isinstance(broker, dict) or broker.get("name") != broker_name:
                continue
            auth = broker.get("auth", {})
            if not isinstance(auth, dict):
                continue
            if "owner" in auth:
                owner = str(auth.get("owner") or "")
            if "email" in auth:
                email = str(auth.get("email") or "")
    return owner, email


def _load_toml_file(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


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


def _lookup_iata_code_with_retry(
    code: str,
    script_version: str = "unknown",
    attempts: int = 3,
) -> tuple[str | None, bool]:
    """Look up an IATA code. Returns (airport name, validation_unavailable)."""
    url = _iata_api_url(f"code={urllib.request.quote(code)}", script_version)
    for attempt in range(1, attempts + 1):
        try:
            data = json.loads(_iata_request(url))
            if not isinstance(data, dict):
                return None, False
            return data.get("name"), False
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, OSError):
            if attempt == attempts:
                return None, True
            time.sleep(1)
    return None, True


def lookup_iata_code(code: str, script_version: str = "unknown") -> str | None:
    """Look up a specific IATA code. Returns airport name or None."""
    name, _validation_unavailable = _lookup_iata_code_with_retry(code, script_version)
    return name


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
            name, validation_unavailable = _lookup_iata_code_with_retry(upper_query, script_version)
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
            if validation_unavailable:
                print_warning(f"Could not validate IATA code '{upper_query}' after retrying")
                if prompt_yes_no(f"Use '{upper_query}' without validation?", "y"):
                    print()
                    print_success(f"Selected: {upper_query}")
                    return upper_query
                print()
                continue
            else:
                print_warning(f"IATA code '{upper_query}' was not found in the LetsMesh database")
                if prompt_yes_no(f"Use '{upper_query}' anyway?", "y"):
                    print()
                    print_success(f"Selected: {upper_query}")
                    return upper_query
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

def configure_custom_broker(broker_num: int, config_dir: str) -> None:
    """Configure a single custom MQTT broker interactively."""
    user_toml = str(migrate_user_config_filename(config_dir))

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
    print("  2) MeshCore Auth Token")
    print("  3) None (anonymous)")
    auth_choice = prompt_input("Choose authentication method [1-3]", "1")

    auth_method = "none"
    username = password = audience = owner = email = ""

    if auth_choice == "2":
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
    user_toml_path = migrate_user_config_filename(ctx.config_dir)
    user_toml = str(user_toml_path)

    # Ensure 99-user.toml exists with base settings
    if not Path(user_toml).exists():
        from .system import select_serial_device
        serial_device = select_serial_device()
        write_user_toml_base(user_toml, "XXX", serial_device, ctx.repo, ctx.branch)

    added_brokers = False
    had_existing_brokers = _config_dir_has_broker(ctx.config_dir)

    while True:
        print()
        print_header("MQTT Broker Configuration")
        print()
        _print_configured_presets(ctx.config_dir)
        print_info("Choose how to configure MQTT brokers:")
        print("  1) Select bundled broker presets")
        print("  2) Import a preset from a URL or local path")
        print("  3) Configure a custom MQTT broker")
        print("  4) Manage existing presets")
        print("  5) Finish without adding or changing brokers")
        print()

        choice = prompt_input("Choose broker setup option [1-5]", "1")

        if choice == "1":
            selected = _select_bundled_presets(ctx)
            if selected:
                for preset in selected:
                    try:
                        copied = copy_preset_to_config(preset, ctx.config_dir)
                        print_success(f"Preset installed: {copied.name}")
                        added_brokers = True
                    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                        print_error(f"Failed to install preset {preset.name}: {exc}")
            else:
                print_warning("No bundled presets selected")
        elif choice == "2":
            source = prompt_input("Preset URL or local path")
            if not source:
                print_warning("No preset source provided")
            else:
                try:
                    copied = import_preset_to_config(source, ctx.config_dir)
                    print_success(f"Preset installed: {copied.name}")
                    added_brokers = True
                except (OSError, ValueError, tomllib.TOMLDecodeError, urllib.error.URLError) as exc:
                    print_error(f"Failed to import preset: {exc}")
        elif choice == "3":
            _configure_iata_simple(user_toml)
            configure_custom_broker(_next_custom_broker_number(ctx.config_dir), ctx.config_dir)
            added_brokers = True
        elif choice == "4":
            _manage_existing_presets(ctx.config_dir)
        elif choice == "5":
            break
        else:
            print_error("Invalid selection")
            continue

        if not prompt_yes_no("Add or manage another broker preset or custom broker?", "n"):
            break

    if token_preset_brokers(ctx.config_dir):
        _configure_iata_for_presets(user_toml, ctx)
        _configure_token_preset_overrides(ctx.config_dir)

    if not added_brokers and not had_existing_brokers:
        print_warning(f"No MQTT brokers configured - you'll need to edit {user_toml} manually")

    # Fix ownership after writing config
    if platform.system() != "Darwin" and ctx.svc_user:
        import shutil as _shutil
        _shutil.chown(user_toml, "root", ctx.svc_user)
        os.chmod(user_toml, 0o644)


def _configure_iata_simple(user_toml: str) -> None:
    """Prompt for simple IATA and update the user TOML."""
    existing_iata = _read_existing_iata(user_toml)
    if not existing_iata or existing_iata == "XXX":
        iata = prompt_iata_simple()
        _update_iata_in_file(user_toml, iata)
        print_success(f"IATA code set to: {iata}")


def _configure_iata_for_presets(user_toml: str, ctx: InstallerContext) -> None:
    """Prompt for IATA for preset brokers if the user config still needs it."""
    existing_iata = _read_existing_iata(user_toml)
    if not existing_iata or existing_iata == "XXX":
        iata = prompt_iata_letsmesh("", ctx.script_version)
        _update_iata_in_file(user_toml, iata)
        print_success(f"IATA code set to: {iata}")


def _configure_token_preset_overrides(config_dir: str) -> None:
    """Configure owner/email overrides separately for each token-auth preset."""
    user_toml = str(user_config_path(config_dir))
    presets = token_preset_brokers(config_dir)
    if not presets:
        return

    print()
    print_info("Token-authenticated broker presets support optional owner identification")
    print_info("This links your observer to your MeshCore public key and email")

    for preset_path, broker_names in presets.items():
        print()
        print_header(f"Owner Info: {preset_path.name}")
        print_info("Token-authenticated brokers in this preset:")

        metadata = {
            broker_name: _broker_auth_metadata(config_dir, broker_name)
            for broker_name in broker_names
        }
        has_owner_info = any(owner or email for owner, email in metadata.values())

        for broker_name in broker_names:
            owner, email = metadata[broker_name]
            print(f"  - {broker_name}")
            print(f"    owner: {owner or '(not set)'}")
            print(f"    email: {email or '(not set)'}")

        if has_owner_info and not prompt_yes_no(f"Change owner info for {preset_path.name}?", "n"):
            continue

        owner_default = _shared_metadata_default(metadata, 0)
        email_default = _shared_metadata_default(metadata, 1)
        owner_pubkey = prompt_owner_pubkey(owner_default)
        owner_email = prompt_owner_email(email_default)
        _rewrite_token_owner_overrides_toml(user_toml, broker_names, owner_pubkey, owner_email)
        print_success(f"Owner info updated for {preset_path.name}")

    allowed_companions = prompt_allowed_companions()
    if allowed_companions:
        append_remote_serial_toml(user_toml, allowed_companions)
        count = len([k for k in allowed_companions.split(",") if k.strip()])
        print_success(f"Remote serial access enabled with {count} companion(s)")


def _shared_metadata_default(metadata: dict[str, tuple[str, str]], idx: int) -> str:
    """Return the shared existing owner/email value if all brokers agree."""
    values = {pair[idx] for pair in metadata.values() if pair[idx]}
    return values.pop() if len(values) == 1 else ""


def _print_configured_presets(config_dir: str) -> None:
    """Print a summary of active preset files."""
    presets = configured_presets(config_dir)
    if not presets:
        print_info("Configured broker presets: none")
        print()
        return

    print_info("Configured broker presets:")
    for preset, broker_names in presets.items():
        brokers = ", ".join(broker_names) if broker_names else "no valid broker blocks"
        print(f"  - {preset.name}: {brokers}")
    print()


def _manage_existing_presets(config_dir: str) -> None:
    """Delete configured preset files and matching user overrides."""
    presets = configured_presets(config_dir)
    if not presets:
        print_warning("No configured presets found")
        return

    print()
    print_info("Configured broker presets:")
    preset_items = list(presets.items())
    for idx, (preset, broker_names) in enumerate(preset_items, 1):
        brokers = ", ".join(broker_names) if broker_names else "no valid broker blocks"
        print(f"  {idx}) {preset.name}: {brokers}")
    print()

    raw = prompt_input(f"Select presets to delete (enter without a selection to return) [1-{len(preset_items)}], comma-separated")
    selected = _parse_number_selection(raw, len(preset_items))
    if not selected:
        print_warning("No presets selected")
        return

    user_toml = user_config_path(config_dir)
    for idx in selected:
        preset, broker_names = preset_items[idx - 1]
        if not prompt_yes_no(f"Delete {preset.name}?", "n"):
            continue
        preset.unlink(missing_ok=True)
        _remove_broker_overrides_toml(user_toml, broker_names)
        print_success(f"Deleted {preset.name} and removed matching user overrides")


def _parse_number_selection(raw: str, max_value: int) -> list[int]:
    """Parse comma-separated numeric selections."""
    selected: list[int] = []
    seen: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            print_warning(f"Ignoring invalid selection: {part}")
            continue
        idx = int(part)
        if idx < 1 or idx > max_value:
            print_warning(f"Ignoring out-of-range selection: {part}")
            continue
        if idx not in seen:
            selected.append(idx)
            seen.add(idx)
    return selected


def _select_bundled_presets(ctx: InstallerContext) -> list[Path]:
    """Prompt for one or more bundled presets."""
    presets = list_bundled_presets(ctx.repo_dir)
    if not presets:
        print_warning("No bundled presets found")
        return []

    default_idx = 1
    for idx, preset in enumerate(presets, 1):
        if preset.name == "letsmesh.toml":
            default_idx = idx
            break

    print()
    print_info("Available broker presets:")
    for idx, preset in enumerate(presets, 1):
        print(f"  {idx}) {preset.name}")
    print()

    raw = prompt_input(f"Select presets [1-{len(presets)}], comma-separated", str(default_idx))
    return [presets[idx - 1] for idx in _parse_number_selection(raw, len(presets))]


def _next_custom_broker_number(config_dir: str) -> int:
    """Choose the next custom broker number based on active config snippets."""
    config_d = Path(config_dir) / "config.d"
    existing_count = 0
    for path in config_d.glob("*.toml"):
        existing_count += path.read_text().count("[[broker]]")
    return existing_count + 1


def _config_dir_has_broker(config_dir: str) -> bool:
    """Return whether any active config drop-in already contains broker blocks."""
    config_d = Path(config_dir) / "config.d"
    if not config_d.is_dir():
        return False
    return any("[[broker]]" in path.read_text() for path in config_d.glob("*.toml"))


# ---------------------------------------------------------------------------
# Update owner info for existing config
# ---------------------------------------------------------------------------

def update_owner_info(config_dir: str) -> None:
    """Update owner public key and email for existing token-auth brokers."""
    user_toml = str(migrate_user_config_filename(config_dir))

    if not Path(user_toml).exists():
        print_error("No configuration file found")
        return

    print()
    print_header("Update Owner Information")

    content = Path(user_toml).read_text()
    has_token_presets = bool(token_preset_brokers(config_dir))
    if 'method = "token"' not in content and not has_token_presets:
        print_warning("No brokers configured with auth token authentication")
        return

    if has_token_presets:
        _configure_token_preset_overrides(config_dir)
        if 'method = "token"' not in content:
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
    """Read the existing IATA code from a user TOML."""
    if not Path(user_toml).exists():
        return ""
    content = Path(user_toml).read_text()
    match = re.search(r'^\s*iata\s*=\s*"([^"]*)"', content, re.MULTILINE)
    return match.group(1) if match else ""


def _update_iata_in_file(user_toml: str, iata: str) -> None:
    """Update the iata value in a user TOML."""
    content = Path(user_toml).read_text()
    content = re.sub(r'^(iata\s*=\s*).*$', f'\\1"{iata}"', content, flags=re.MULTILINE)
    Path(user_toml).write_text(content)


# Need platform for the import in configure_mqtt_brokers
import platform
