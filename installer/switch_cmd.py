"""Switch an existing install to the CascadiaMesh version (MediaMesh support).

`python3 -m installer switch` (scripts/switch-to-cascadiamesh.sh runs it):
points the user config's [update] section at this repository, so every later
update stays on it, adds the CascadiaMesh analyzer's broker preset unless one
is already configured (--no-broker skips it), then updates as usual. On a
repeater without a second radio profile the result behaves exactly like the
regular meshcoretomqtt.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from .config import (
    PRESET_PREFIX,
    migrate_user_config_filename,
    toml_escape,
)
from .ui import print_header, print_info, print_success, print_warning

if TYPE_CHECKING:
    from . import InstallerContext

CASCADIAMESH_REPO = "jade-on-mesh/meshcoretomqtt"
CASCADIAMESH_BRANCH = "radio2-profile"
CASCADIAMESH_SERVER = "mqtt-v1.cascadiamesh.org"
PRESET_NAME = "cascadiamesh.toml"

_TABLE_RE = re.compile(r"^\s*\[")
_UPDATE_RE = re.compile(r"^\s*\[\s*update\s*\]\s*(#.*)?$")


def set_update_source(text: str, repo: str, branch: str) -> str:
    """The user config with [update] repo and branch set: the lines in an
    existing [update] table are changed (or added to it), else the table is
    appended. Everything else, comments too, is kept as it is."""
    repo_line = f'repo = "{toml_escape(repo)}"'
    branch_line = f'branch = "{toml_escape(branch)}"'
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if _UPDATE_RE.match(l)), None)
    if start is None:
        out = text.rstrip("\n")
        return (out + "\n\n" if out else "") + f"[update]\n{repo_line}\n{branch_line}\n"
    end = next((i for i in range(start + 1, len(lines)) if _TABLE_RE.match(lines[i])), len(lines))
    have = {"repo": False, "branch": False}
    for i in range(start + 1, end):
        m = re.match(r"^\s*(repo|branch)\s*=", lines[i])
        if m:
            key = m.group(1)
            lines[i] = repo_line if key == "repo" else branch_line
            have[key] = True
    insert = [l for k, l in (("repo", repo_line), ("branch", branch_line)) if not have[k]]
    lines[start + 1:start + 1] = insert
    return "\n".join(lines) + "\n"


def has_cascadiamesh_broker(config_dir: str | Path) -> bool:
    """Whether any config file already sends to the CascadiaMesh broker."""
    for path in sorted((Path(config_dir) / "config.d").glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for broker in data.get("broker", []) or []:
            if isinstance(broker, dict) and broker.get("server") == CASCADIAMESH_SERVER and broker.get("enabled", True):
                return True
    return False


def add_cascadiamesh_preset(config_dir: str | Path, preset_text: str) -> Path:
    """Writes the CascadiaMesh broker preset into config.d."""
    dest = Path(config_dir) / "config.d" / f"{PRESET_PREFIX}{PRESET_NAME}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(preset_text)
    return dest


def switch_config(config_dir: str | Path, repo: str, branch: str, preset_text: str | None) -> dict:
    """Does the config part of the switch; returns what it did."""
    user_toml = migrate_user_config_filename(config_dir)
    before = user_toml.read_text() if user_toml.exists() else ""
    after = set_update_source(before, repo, branch)
    tomllib.loads(after)  # never leave a config that won't load
    user_toml.parent.mkdir(parents=True, exist_ok=True)
    user_toml.write_text(after)
    added = None
    if preset_text is not None and not has_cascadiamesh_broker(config_dir):
        added = add_cascadiamesh_preset(config_dir, preset_text)
    return {"user_toml": user_toml, "preset": added}


def run_switch(ctx: InstallerContext, add_broker: bool = True) -> None:
    from .update_cmd import run_update

    print_header("Switching to the CascadiaMesh version of MeshCore to MQTT")
    if not Path(ctx.install_dir, "mctomqtt.py").exists():
        print_warning(f"No installation found at {ctx.install_dir}. Install it first:")
        print_info(f"curl -fsSL https://raw.githubusercontent.com/{ctx.repo}/{ctx.branch}/install.sh | "
                   f"sudo bash -s -- --repo {ctx.repo} --branch {ctx.branch}")
        raise SystemExit(1)
    preset = None
    if add_broker:
        src = Path(ctx.repo_dir or ".", "presets", PRESET_NAME)
        preset = src.read_text() if src.exists() else None
    done = switch_config(ctx.config_dir, ctx.repo, ctx.branch, preset)
    print_success(f"Updates now come from {ctx.repo} ({ctx.branch}): {done['user_toml']}")
    if done["preset"]:
        print_success(f"Added the CascadiaMesh analyzer's broker: {done['preset']}")
    elif add_broker:
        print_info("The CascadiaMesh analyzer's broker was already configured.")
    ctx.update_mode = True
    run_update(ctx)
    print()
    print_info("For MediaMesh, the repeater needs a second radio profile (radio2) and its USB packet log on:")
    print_info("  set usb.logging on")
    print_info("  set usb.logging.always on")
    print_info("Your observer shows on https://analyzer.cascadiamesh.org/#/observers within a few minutes.")
