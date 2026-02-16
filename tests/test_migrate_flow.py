"""Tier 5: Full migration end-to-end flow.

Tests the full env-to-toml migration pipeline with real files.
"""

import tomllib
from pathlib import Path

import pytest

from installer.migrate_cmd import (
    env_to_toml,
    is_already_migrated,
    mark_migrated,
    parse_env_file,
)

pytestmark = pytest.mark.e2e


class TestMigrateFlow:
    def test_full_migration_pipeline(self, tmp_path):
        """Simulate a legacy installation and migrate to TOML."""
        # Create fake legacy installation
        old_dir = tmp_path / ".meshcoretomqtt"
        old_dir.mkdir()

        # Write a .env file (repo defaults)
        env_content = (
            "MCTOMQTT_IATA=XXX\n"
            "MCTOMQTT_LOG_LEVEL=INFO\n"
            "MCTOMQTT_SYNC_TIME=true\n"
            "MCTOMQTT_SERIAL_PORTS=/dev/ttyACM0\n"
            "MCTOMQTT_SERIAL_BAUD_RATE=115200\n"
            "MCTOMQTT_MQTT1_ENABLED=true\n"
            "MCTOMQTT_MQTT1_SERVER=mqtt-us-v1.letsmesh.net\n"
            "MCTOMQTT_MQTT1_PORT=443\n"
            "MCTOMQTT_MQTT1_TRANSPORT=websockets\n"
            "MCTOMQTT_MQTT1_USE_TLS=true\n"
            "MCTOMQTT_MQTT1_USE_AUTH_TOKEN=true\n"
            "MCTOMQTT_MQTT1_TOKEN_AUDIENCE=mqtt-us-v1.letsmesh.net\n"
        )
        (old_dir / ".env").write_text(env_content)

        # Write a .env.local (user overrides)
        env_local_content = (
            "MCTOMQTT_IATA=SEA\n"
            "MCTOMQTT_LOG_LEVEL=DEBUG\n"
            "MCTOMQTT_MQTT1_TOKEN_OWNER=ABCD0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789AB\n"
            "MCTOMQTT_MQTT1_TOKEN_EMAIL=test@example.com\n"
            "MCTOMQTT_MQTT2_ENABLED=true\n"
            "MCTOMQTT_MQTT2_SERVER=mqtt.custom.com\n"
            "MCTOMQTT_MQTT2_PORT=1883\n"
            "MCTOMQTT_MQTT2_USERNAME=myuser\n"
            "MCTOMQTT_MQTT2_PASSWORD=mypass\n"
        )
        (old_dir / ".env.local").write_text(env_local_content)

        # Parse both files
        env = parse_env_file(str(old_dir / ".env"))
        env_local = parse_env_file(str(old_dir / ".env.local"))

        # Merge (env.local overrides env)
        merged = {}
        merged.update(env)
        merged.update(env_local)

        # Convert to TOML
        toml_content = env_to_toml(merged)

        # Write to new location
        config_dir = tmp_path / "etc" / "mctomqtt" / "config.d"
        config_dir.mkdir(parents=True)
        user_toml = config_dir / "00-user.toml"
        user_toml.write_text(
            "# MeshCore to MQTT - User Configuration\n"
            "# Migrated from legacy .env/.env.local installation\n\n"
            + toml_content
        )

        # Verify migrated TOML is valid
        with open(user_toml, "rb") as f:
            data = tomllib.load(f)

        assert data["general"]["iata"] == "SEA"
        assert data["general"]["log_level"] == "DEBUG"
        assert data["serial"]["ports"] == ["/dev/ttyACM0"]

        brokers = data["broker"]
        assert len(brokers) == 2
        assert brokers[0]["name"] == "letsmesh-us"
        assert brokers[0]["auth"]["method"] == "token"
        assert brokers[0]["auth"]["email"] == "test@example.com"
        assert brokers[1]["name"] == "custom-2"
        assert brokers[1]["auth"]["method"] == "password"
        assert brokers[1]["auth"]["username"] == "myuser"

    def test_empty_legacy_produces_valid_toml(self, tmp_path):
        """Empty legacy .env files produce valid (empty) TOML."""
        old_dir = tmp_path / ".meshcoretomqtt"
        old_dir.mkdir()
        (old_dir / ".env").write_text("")
        (old_dir / ".env.local").write_text("")

        env = parse_env_file(str(old_dir / ".env"))
        env_local = parse_env_file(str(old_dir / ".env.local"))

        merged = {}
        merged.update(env)
        merged.update(env_local)

        toml_content = env_to_toml(merged)
        parsed = tomllib.loads(toml_content)
        assert isinstance(parsed, dict)


class TestMigrateIdempotency:
    def test_not_migrated_by_default(self, tmp_path):
        """A fresh legacy directory is not marked as migrated."""
        old_dir = tmp_path / ".meshcoretomqtt"
        old_dir.mkdir()
        assert is_already_migrated(str(old_dir)) is False

    def test_mark_migrated_creates_sentinel(self, tmp_path):
        """mark_migrated writes a sentinel file with the install path."""
        old_dir = tmp_path / ".meshcoretomqtt"
        old_dir.mkdir()
        mark_migrated(str(old_dir), "/opt/mctomqtt")

        sentinel = old_dir / ".migrated"
        assert sentinel.exists()
        assert "/opt/mctomqtt" in sentinel.read_text()

    def test_is_already_migrated_after_mark(self, tmp_path):
        """is_already_migrated returns True after mark_migrated is called."""
        old_dir = tmp_path / ".meshcoretomqtt"
        old_dir.mkdir()
        mark_migrated(str(old_dir), "/opt/mctomqtt")
        assert is_already_migrated(str(old_dir)) is True

    def test_mark_migrated_is_idempotent(self, tmp_path):
        """Calling mark_migrated twice does not error."""
        old_dir = tmp_path / ".meshcoretomqtt"
        old_dir.mkdir()
        mark_migrated(str(old_dir), "/opt/mctomqtt")
        mark_migrated(str(old_dir), "/opt/mctomqtt")
        assert is_already_migrated(str(old_dir)) is True
