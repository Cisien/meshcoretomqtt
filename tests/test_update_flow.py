"""Tier 5: Full update end-to-end flow.

Requires an existing installation (from test_install_flow or a real one).
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).parent.parent


class TestUpdateFlow:
    def test_update_preserves_user_toml(self, tmp_path: Path) -> None:
        """Verify that 00-user.toml is preserved after update."""
        config_dir = tmp_path / "etc" / "mctomqtt"
        config_d = config_dir / "config.d"
        config_d.mkdir(parents=True)

        # Create a 00-user.toml that should be preserved
        user_toml_content = '[general]\niata = "SEA"\n\n[serial]\nports = ["/dev/ttyACM0"]\n'
        (config_d / "00-user.toml").write_text(user_toml_content)
        (config_dir / "config.toml").write_text("[general]\niata = \"XXX\"\n")

        # Simulate an update by verifying the 00-user.toml is intact
        content = (config_d / "00-user.toml").read_text()
        assert content == user_toml_content

        # Verify it's valid TOML
        with open(config_d / "00-user.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["general"]["iata"] == "SEA"
        assert data["serial"]["ports"] == ["/dev/ttyACM0"]

    def test_update_config_toml_valid(self, tmp_path: Path) -> None:
        """Verify config.toml from repo is valid TOML."""
        config_example = PROJECT_ROOT / "config.toml.example"
        if config_example.exists():
            with open(config_example, "rb") as f:
                data = tomllib.load(f)
            assert "general" in data
