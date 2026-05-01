"""Tier 2: Tests for TOML file writing/reading/updating with real temp files.

Uses tomllib to verify output is valid TOML that parses to expected structures.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from config_loader import merge_broker_lists
from installer import InstallerContext
from installer.config import (
    _read_existing_iata,
    _read_remote_serial_companions,
    _rewrite_token_owner_overrides_toml,
    _set_remote_serial,
    _select_bundled_presets,
    _toml_dumps,
    _update_iata_in_file,
    append_custom_broker_toml,
    append_disabled_broker_toml,
    append_letsmesh_broker_toml,
    append_remote_serial_toml,
    append_token_owner_overrides_toml,
    configured_presets,
    copy_preset_to_config,
    import_preset_to_config,
    list_bundled_presets,
    migrate_user_config_filename,
    preset_dest_path,
    _manage_existing_presets,
    token_broker_names_from_preset,
    validate_preset_toml,
    write_user_toml_base,
)

class TestWriteUserTomlBase:
    def test_creates_file_with_sections(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "Cisien/meshcoretomqtt", "main")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["general"]["iata"] == "SEA"
        assert data["serial"]["ports"] == ["/dev/ttyACM0"]
        assert data["update"]["repo"] == "Cisien/meshcoretomqtt"
        assert data["update"]["branch"] == "main"

    def test_special_chars_escaped(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, 'S"A', "/dev/t\\y", "repo", "branch")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["general"]["iata"] == 'S"A'
        assert data["serial"]["ports"] == ["/dev/t\\y"]


class TestAppendLetsmeshBrokerToml:
    def test_appends_valid_broker_block(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_letsmesh_broker_toml(
            dest, "letsmesh-us", "mqtt-us-v1.letsmesh.net",
            "mqtt-us-v1.letsmesh.net", "OWNER123", "test@example.com",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        brokers = data["broker"]
        assert len(brokers) == 1
        assert brokers[0]["name"] == "letsmesh-us"
        assert brokers[0]["server"] == "mqtt-us-v1.letsmesh.net"
        assert brokers[0]["port"] == 443
        assert brokers[0]["transport"] == "websockets"
        assert brokers[0]["tls"]["enabled"] is True
        assert brokers[0]["auth"]["method"] == "token"
        assert brokers[0]["auth"]["owner"] == "OWNER123"
        assert brokers[0]["auth"]["email"] == "test@example.com"

    def test_two_brokers_us_eu(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_letsmesh_broker_toml(
            dest, "letsmesh-us", "mqtt-us-v1.letsmesh.net",
            "mqtt-us-v1.letsmesh.net", "", "",
        )
        append_letsmesh_broker_toml(
            dest, "letsmesh-eu", "mqtt-eu-v1.letsmesh.net",
            "mqtt-eu-v1.letsmesh.net", "", "",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert len(data["broker"]) == 2
        assert data["broker"][0]["name"] == "letsmesh-us"
        assert data["broker"][1]["name"] == "letsmesh-eu"


class TestAppendDisabledBrokerToml:
    def test_produces_valid_toml(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_disabled_broker_toml(dest, "letsmesh-us")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        broker = data["broker"][0]
        assert broker["name"] == "letsmesh-us"
        assert broker["enabled"] is False

    def test_disable_both_letsmesh_brokers(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_disabled_broker_toml(dest, "letsmesh-us")
        append_disabled_broker_toml(dest, "letsmesh-eu")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert len(data["broker"]) == 2
        assert all(b["enabled"] is False for b in data["broker"])

    def test_override_merges_with_base_config(self, tmp_path: Path) -> None:
        """Disabled override merges into base config and disables the broker."""
        # Write a base config with both brokers enabled
        base = tmp_path / "config.toml"
        base.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = true\n'
            'server = "mqtt-us-v1.letsmesh.net"\n\n'
            '[[broker]]\nname = "letsmesh-eu"\nenabled = true\n'
            'server = "mqtt-eu-v1.letsmesh.net"\n'
        )

        # Write an override that disables letsmesh-us
        override = tmp_path / "00-user.toml"
        write_user_toml_base(str(override), "SEA", "/dev/ttyACM0", "repo", "main")
        append_disabled_broker_toml(str(override), "letsmesh-us")

        with open(base, "rb") as f:
            base_data = tomllib.load(f)
        with open(override, "rb") as f:
            override_data = tomllib.load(f)

        result = merge_broker_lists(base_data["broker"], override_data["broker"])

        assert len(result) == 2
        assert result[0]["name"] == "letsmesh-us"
        assert result[0]["enabled"] is False
        assert result[0]["server"] == "mqtt-us-v1.letsmesh.net"
        assert result[1]["name"] == "letsmesh-eu"
        assert result[1]["enabled"] is True


class TestAppendCustomBrokerToml:
    def test_password_auth(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_custom_broker_toml(
            dest, "custom-1", "mqtt.example.com", "1883", "tcp",
            "false", "true", "password",
            username="user", password="pass",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        broker = data["broker"][0]
        assert broker["auth"]["method"] == "password"
        assert broker["auth"]["username"] == "user"
        assert broker["auth"]["password"] == "pass"

    def test_token_auth(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_custom_broker_toml(
            dest, "custom-1", "mqtt.example.com", "8883", "websockets",
            "true", "true", "token",
            audience="aud", owner="owner", email="e@x.com",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        broker = data["broker"][0]
        assert broker["auth"]["method"] == "token"
        assert broker["auth"]["audience"] == "aud"
        assert broker["auth"]["owner"] == "owner"
        assert broker["auth"]["email"] == "e@x.com"

    def test_no_auth(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_custom_broker_toml(
            dest, "custom-1", "mqtt.example.com", "1883", "tcp",
            "false", "true", "none",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["broker"][0]["auth"]["method"] == "none"

    def test_tls_enabled(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_custom_broker_toml(
            dest, "custom-1", "mqtt.example.com", "8883", "tcp",
            "true", "true", "none",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["broker"][0]["tls"]["enabled"] is True
        assert data["broker"][0]["tls"]["verify"] is True

    def test_tls_disabled(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_custom_broker_toml(
            dest, "custom-1", "mqtt.example.com", "1883", "tcp",
            "false", "true", "none",
        )

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert "tls" not in data["broker"][0]


class TestAppendRemoteSerialToml:
    def test_with_companions(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_remote_serial_toml(dest, "KEY1,KEY2")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["remote_serial"]["enabled"] is True
        assert data["remote_serial"]["allowed_companions"] == ["KEY1", "KEY2"]

    def test_empty_companions(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_remote_serial_toml(dest, "")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["remote_serial"]["enabled"] is False
        assert data["remote_serial"]["allowed_companions"] == []


class TestUserConfigMigration:
    def test_renames_legacy_user_config(self, tmp_path: Path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        legacy = config_d / "00-user.toml"
        legacy.write_text('[general]\niata = "SEA"\n')

        result = migrate_user_config_filename(tmp_path)

        assert result == config_d / "99-user.toml"
        assert result.read_text() == '[general]\niata = "SEA"\n'
        assert not legacy.exists()

    def test_conflict_aborts_when_both_user_configs_exist(self, tmp_path: Path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "00-user.toml").write_text('[general]\niata = "SEA"\n')
        (config_d / "99-user.toml").write_text('[general]\niata = "LAX"\n')

        with pytest.raises(SystemExit):
            migrate_user_config_filename(tmp_path)


class TestPresetFiles:
    def test_preset_dest_path_prefixes_filename(self, tmp_path: Path) -> None:
        assert preset_dest_path(tmp_path, "letsmesh.toml") == tmp_path / "config.d" / "10-letsmesh.toml"

    def test_letsmesh_preset_is_prompt_default(self, tmp_path: Path) -> None:
        preset_dir = tmp_path / "presets"
        preset_dir.mkdir()
        (preset_dir / "custom.toml").write_text('[[broker]]\nname = "custom"\n')
        (preset_dir / "letsmesh.toml").write_text('[[broker]]\nname = "letsmesh-us"\n')

        ctx = InstallerContext(repo_dir=str(tmp_path))
        with patch("installer.config.prompt_input", side_effect=lambda _prompt, default: default) as prompt:
            selected = _select_bundled_presets(ctx)

        prompt.assert_called_once_with("Select presets [1-2], comma-separated", "2")
        assert [preset.name for preset in selected] == ["letsmesh.toml"]

    def test_validate_valid_preset(self, tmp_path: Path) -> None:
        preset = tmp_path / "letsmesh.toml"
        preset.write_text('[[broker]]\nname = "letsmesh-us"\nserver = "mqtt.example"\n')

        data = validate_preset_toml(preset)

        assert data["broker"][0]["name"] == "letsmesh-us"

    def test_rejects_preset_without_broker(self, tmp_path: Path) -> None:
        preset = tmp_path / "empty.toml"
        preset.write_text("[general]\niata = \"SEA\"\n")

        with pytest.raises(ValueError):
            validate_preset_toml(preset)

    def test_rejects_preset_without_broker_name(self, tmp_path: Path) -> None:
        preset = tmp_path / "bad.toml"
        preset.write_text('[[broker]]\nserver = "mqtt.example"\n')

        with pytest.raises(ValueError):
            validate_preset_toml(preset)

    def test_rejects_invalid_toml(self, tmp_path: Path) -> None:
        preset = tmp_path / "bad.toml"
        preset.write_text("[[broker]\n")

        with pytest.raises(tomllib.TOMLDecodeError):
            validate_preset_toml(preset)

    def test_rejects_unsafe_filename(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            preset_dest_path(tmp_path, "../evil.toml")

    def test_copy_preset_uses_prefixed_name(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "etc"
        source = tmp_path / "letsmesh.toml"
        source.write_text('[[broker]]\nname = "letsmesh-us"\n')

        copied = copy_preset_to_config(source, config_dir)

        assert copied == config_dir / "config.d" / "10-letsmesh.toml"
        with open(copied, "rb") as f:
            data = tomllib.load(f)
        assert data["broker"][0]["name"] == "letsmesh-us"

    def test_import_local_preset(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "etc"
        source = tmp_path / "community.toml"
        source.write_text('[[broker]]\nname = "community"\n')

        copied = import_preset_to_config(str(source), config_dir)

        assert copied == config_dir / "config.d" / "10-community.toml"
        assert copied.exists()

    def test_token_broker_names_from_preset(self, tmp_path: Path) -> None:
        preset = tmp_path / "token.toml"
        preset.write_text(
            '[[broker]]\nname = "token-broker"\n[broker.auth]\nmethod = "token"\n'
            '\n[[broker]]\nname = "anon"\n[broker.auth]\nmethod = "none"\n'
        )

        assert token_broker_names_from_preset(preset) == [("token.toml", "token-broker")]

    def test_configured_presets_lists_active_preset_brokers(self, tmp_path: Path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "10-letsmesh.toml").write_text(
            '[[broker]]\nname = "letsmesh-us"\n'
            '\n[[broker]]\nname = "letsmesh-eu"\n'
        )

        result = configured_presets(tmp_path)

        assert result[config_d / "10-letsmesh.toml"] == ["letsmesh-us", "letsmesh-eu"]

    def test_manage_existing_presets_deletes_file_and_matching_overrides(self, tmp_path: Path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        preset = config_d / "10-letsmesh.toml"
        preset.write_text(
            '[[broker]]\nname = "letsmesh-us"\n'
            '\n[[broker]]\nname = "letsmesh-eu"\n'
        )
        user_toml = config_d / "99-user.toml"
        user_toml.write_text(
            '[general]\niata = "SEA"\n\n'
            '[[broker]]\nname = "letsmesh-us"\n[broker.auth]\nowner = "OWNER"\n'
            '\n[[broker]]\nname = "custom-override"\nserver = "mqtt.example.com"\n'
        )

        with (
            patch("installer.config.prompt_input", return_value="1"),
            patch("installer.config.prompt_yes_no", return_value=True),
        ):
            _manage_existing_presets(str(tmp_path))

        content = user_toml.read_text()
        assert not preset.exists()
        assert 'name = "letsmesh-us"' not in content
        assert 'name = "custom-override"' in content
        assert 'server = "mqtt.example.com"' in content


class TestAppendTokenOwnerOverridesToml:
    def test_appends_valid_owner_overrides(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "99-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        append_token_owner_overrides_toml(dest, ["letsmesh-us"], "OWNER123", "test@example.com")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        broker = data["broker"][0]
        assert broker["name"] == "letsmesh-us"
        assert broker["auth"]["owner"] == "OWNER123"
        assert broker["auth"]["email"] == "test@example.com"


class TestReadExistingIata:
    def test_reads_iata(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        assert _read_existing_iata(dest) == "SEA"

    def test_no_iata(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        (tmp_path / "00-user.toml").write_text("[serial]\nports = []\n")
        assert _read_existing_iata(dest) == ""

    def test_nonexistent_file(self) -> None:
        assert _read_existing_iata("/nonexistent/00-user.toml") == ""


class TestUpdateIataInFile:
    def test_updates_iata(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        _update_iata_in_file(dest, "LAX")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["general"]["iata"] == "LAX"

    def test_preserves_other_content(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "00-user.toml")
        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "repo", "main")
        _update_iata_in_file(dest, "LAX")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        assert data["serial"]["ports"] == ["/dev/ttyACM0"]
        assert data["update"]["repo"] == "repo"


class TestFullComposition:
    def test_complete_config_roundtrip(self, tmp_path: Path) -> None:
        """Build a complete config and verify everything via tomllib."""
        dest = str(tmp_path / "00-user.toml")

        write_user_toml_base(dest, "SEA", "/dev/ttyACM0", "Cisien/meshcoretomqtt", "main")
        append_letsmesh_broker_toml(
            dest, "letsmesh-us", "mqtt-us-v1.letsmesh.net",
            "mqtt-us-v1.letsmesh.net", "ABCD" * 16, "test@example.com",
        )
        append_letsmesh_broker_toml(
            dest, "letsmesh-eu", "mqtt-eu-v1.letsmesh.net",
            "mqtt-eu-v1.letsmesh.net", "ABCD" * 16, "test@example.com",
        )
        append_remote_serial_toml(dest, "KEY1,KEY2")

        with open(dest, "rb") as f:
            data = tomllib.load(f)

        # General
        assert data["general"]["iata"] == "SEA"

        # Serial
        assert data["serial"]["ports"] == ["/dev/ttyACM0"]

        # Update
        assert data["update"]["repo"] == "Cisien/meshcoretomqtt"
        assert data["update"]["branch"] == "main"

        # Brokers
        assert len(data["broker"]) == 2
        assert data["broker"][0]["name"] == "letsmesh-us"
        assert data["broker"][0]["server"] == "mqtt-us-v1.letsmesh.net"
        assert data["broker"][0]["auth"]["method"] == "token"
        assert data["broker"][1]["name"] == "letsmesh-eu"
        assert data["broker"][1]["server"] == "mqtt-eu-v1.letsmesh.net"

        # Remote serial
        assert data["remote_serial"]["enabled"] is True
        assert data["remote_serial"]["allowed_companions"] == ["KEY1", "KEY2"]


class TestTomlDumpsRoundTrip:
    """The generic TOML serializer must preserve any structure tomllib can parse."""

    def _roundtrip(self, source: str) -> dict:
        original = tomllib.loads(source)
        rendered = _toml_dumps(original)
        return tomllib.loads(rendered)

    def test_preserves_unknown_top_level_section(self) -> None:
        """A user's [topics] override must survive a load/modify/dump cycle."""
        source = (
            '[general]\niata = "SEA"\n\n'
            '[topics]\nstatus = "custom/{IATA}/status"\npackets = "custom/{IATA}/packets"\n'
        )
        result = self._roundtrip(source)
        assert result["topics"]["status"] == "custom/{IATA}/status"
        assert result["topics"]["packets"] == "custom/{IATA}/packets"

    def test_preserves_unknown_broker_subsection(self) -> None:
        """Future broker subsections (e.g. [broker.metrics]) must round-trip."""
        source = (
            '[[broker]]\nname = "x"\n'
            '[broker.metrics]\nenabled = true\ninterval = 30\n'
        )
        result = self._roundtrip(source)
        assert result["broker"][0]["metrics"] == {"enabled": True, "interval": 30}

    def test_preserves_arbitrary_nested_tables(self) -> None:
        """Three-level nesting and deeply nested tables stay intact."""
        source = '[a.b.c]\nkey = "value"\n[a.b.c.d]\nflag = false\n'
        result = self._roundtrip(source)
        assert result["a"]["b"]["c"]["key"] == "value"
        assert result["a"]["b"]["c"]["d"]["flag"] is False

    def test_preserves_value_types(self) -> None:
        source = (
            '[s]\n'
            'a_str = "hello"\n'
            'a_int = 42\n'
            'a_neg = -7\n'
            'a_float = 3.14\n'
            'a_bool_t = true\n'
            'a_bool_f = false\n'
            'a_arr = [1, 2, 3]\n'
            'a_str_arr = ["x", "y"]\n'
        )
        result = self._roundtrip(source)
        assert result["s"]["a_str"] == "hello"
        assert result["s"]["a_int"] == 42
        assert result["s"]["a_neg"] == -7
        assert result["s"]["a_float"] == 3.14
        assert result["s"]["a_bool_t"] is True
        assert result["s"]["a_bool_f"] is False
        assert result["s"]["a_arr"] == [1, 2, 3]
        assert result["s"]["a_str_arr"] == ["x", "y"]

    def test_escapes_strings_with_special_characters(self) -> None:
        source = '[s]\npath = "C:\\\\Users\\\\foo"\nquote = "she said \\"hi\\""\n'
        result = self._roundtrip(source)
        assert result["s"]["path"] == "C:\\Users\\foo"
        assert result["s"]["quote"] == 'she said "hi"'

    def test_preserves_array_of_tables_order(self) -> None:
        source = (
            '[[broker]]\nname = "first"\n\n'
            '[[broker]]\nname = "second"\n\n'
            '[[broker]]\nname = "third"\n'
        )
        result = self._roundtrip(source)
        assert [b["name"] for b in result["broker"]] == ["first", "second", "third"]

    def test_quotes_keys_that_are_not_bare(self) -> None:
        data = {"general": {"weird key": "value"}}
        rendered = _toml_dumps(data)
        # Round-trip back through tomllib to confirm the quoting is valid
        assert tomllib.loads(rendered)["general"]["weird key"] == "value"


class TestRewriteTokenOwnerOverridesPreservesData:
    """Issue #1 regression tests — the rewriter must not drop unknown sections."""

    def test_preserves_topics_section(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[general]\niata = "SEA"\n\n'
            '[topics]\nstatus = "custom/{IATA}/status"\n\n'
            '[[broker]]\nname = "letsmesh-us"\n'
        )

        _rewrite_token_owner_overrides_toml(
            str(user_toml), ["letsmesh-us"], "OWNER123", "owner@example.com"
        )

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["topics"]["status"] == "custom/{IATA}/status"
        assert data["broker"][0]["auth"]["owner"] == "OWNER123"

    def test_preserves_unknown_broker_subsection(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[[broker]]\nname = "letsmesh-us"\n'
            '[broker.metrics]\nenabled = true\n'
        )

        _rewrite_token_owner_overrides_toml(
            str(user_toml), ["letsmesh-us"], "OWNER", ""
        )

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["broker"][0]["metrics"] == {"enabled": True}
        assert data["broker"][0]["auth"]["owner"] == "OWNER"

    def test_preserves_other_top_level_scalars(self, tmp_path: Path) -> None:
        """A future top-level [foo] section with arbitrary keys must round-trip."""
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[general]\niata = "SEA"\n\n'
            '[future_feature]\nlevel = 5\nname = "experimental"\n\n'
            '[[broker]]\nname = "letsmesh-us"\n'
        )

        _rewrite_token_owner_overrides_toml(
            str(user_toml), ["letsmesh-us"], "OWNER", ""
        )

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["future_feature"] == {"level": 5, "name": "experimental"}


class TestSetRemoteSerial:
    """Issue #2 regression tests — [remote_serial] mutations must be in-place, not appended."""

    def test_creates_section_when_absent(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text('[general]\niata = "SEA"\n')

        _set_remote_serial(str(user_toml), "KEY1,KEY2")

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["remote_serial"]["enabled"] is True
        assert data["remote_serial"]["allowed_companions"] == ["KEY1", "KEY2"]

    def test_replaces_existing_section_in_place(self, tmp_path: Path) -> None:
        """Calling _set_remote_serial twice must NOT duplicate [remote_serial]."""
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[general]\niata = "SEA"\n\n'
            '[remote_serial]\nenabled = true\nallowed_companions = ["OLD"]\n'
        )

        _set_remote_serial(str(user_toml), "NEW1,NEW2")

        # File must be valid TOML (a duplicate [remote_serial] would raise here)
        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["remote_serial"]["allowed_companions"] == ["NEW1", "NEW2"]
        # Confirm the literal text only contains one [remote_serial] header
        assert user_toml.read_text().count("[remote_serial]") == 1

    def test_disables_when_companions_empty(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[remote_serial]\nenabled = true\nallowed_companions = ["KEY"]\n'
        )

        _set_remote_serial(str(user_toml), "")

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["remote_serial"]["enabled"] is False
        assert data["remote_serial"]["allowed_companions"] == []

    def test_repeated_calls_keep_file_valid(self, tmp_path: Path) -> None:
        """Many successive sets must keep the file parseable and idempotent."""
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text('[general]\niata = "SEA"\n')

        for csv in ("A,B", "C", "", "D,E,F", "D,E,F"):
            _set_remote_serial(str(user_toml), csv)

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["remote_serial"]["allowed_companions"] == ["D", "E", "F"]
        assert user_toml.read_text().count("[remote_serial]") == 1

    def test_preserves_other_sections(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[general]\niata = "SEA"\n\n'
            '[topics]\nstatus = "custom/{IATA}"\n\n'
            '[[broker]]\nname = "custom"\nserver = "mqtt.example"\n'
        )

        _set_remote_serial(str(user_toml), "KEY1")

        with open(user_toml, "rb") as f:
            data = tomllib.load(f)
        assert data["general"]["iata"] == "SEA"
        assert data["topics"]["status"] == "custom/{IATA}"
        assert data["broker"][0]["name"] == "custom"
        assert data["remote_serial"]["allowed_companions"] == ["KEY1"]


class TestReadRemoteSerialCompanions:
    def test_returns_csv_of_existing_companions(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text(
            '[remote_serial]\nenabled = true\nallowed_companions = ["A", "B", "C"]\n'
        )
        assert _read_remote_serial_companions(user_toml) == "A,B,C"

    def test_empty_when_section_missing(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "99-user.toml"
        user_toml.write_text('[general]\niata = "SEA"\n')
        assert _read_remote_serial_companions(user_toml) == ""

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        assert _read_remote_serial_companions(tmp_path / "missing.toml") == ""
