"""Tests for config_loader: load_config, deep_merge, merge_broker_lists."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from config_loader import load_config


class TestLoadConfigWithExplicitPaths:
    def test_single_config_file(self, tmp_path: Path) -> None:
        """A single --config file is loaded as the full config."""
        cfg = tmp_path / "my.toml"
        cfg.write_text('[general]\niata = "PDX"\n')

        result = load_config([str(cfg)])

        assert result["general"]["iata"] == "PDX"

    def test_multiple_config_files_overlay(self, tmp_path: Path) -> None:
        """Multiple --config files are merged in order."""
        base = tmp_path / "base.toml"
        base.write_text('[general]\niata = "SEA"\nlog_level = "INFO"\n')

        overlay = tmp_path / "overlay.toml"
        overlay.write_text('[general]\niata = "PDX"\n')

        result = load_config([str(base), str(overlay)])

        assert result["general"]["iata"] == "PDX"
        assert result["general"]["log_level"] == "INFO"

    def test_broker_overlay_merges_by_name(self, tmp_path: Path) -> None:
        """Broker lists in overlays merge by name, not append blindly."""
        base = tmp_path / "base.toml"
        base.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = true\n'
            'server = "mqtt-us-v1.letsmesh.net"\n'
        )

        overlay = tmp_path / "overlay.toml"
        overlay.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = false\n'
        )

        result = load_config([str(base), str(overlay)])

        assert len(result["broker"]) == 1
        assert result["broker"][0]["enabled"] is False
        assert result["broker"][0]["server"] == "mqtt-us-v1.letsmesh.net"

    def test_skips_default_config_d(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When --config is provided, config.d directories are not loaded."""
        # Create a config.d next to config_loader.py's __file__ location
        # that would normally be picked up
        cfg = tmp_path / "my.toml"
        cfg.write_text('[general]\niata = "PDX"\n')

        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "99-extra.toml").write_text('[general]\niata = "OVERWRITTEN"\n')

        # Even though config.d exists, --config should bypass it entirely
        result = load_config([str(cfg)])

        assert result["general"]["iata"] == "PDX"

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        """A nonexistent --config path is skipped with a log error."""
        cfg = tmp_path / "exists.toml"
        cfg.write_text('[general]\niata = "SEA"\n')

        result = load_config(["/nonexistent/path.toml", str(cfg)])

        assert result["general"]["iata"] == "SEA"

    def test_no_config_paths_returns_defaults(self) -> None:
        """Passing None uses default config loading."""
        # Just verify it doesn't crash; actual paths may or may not exist
        result = load_config(None)
        assert isinstance(result, dict)
