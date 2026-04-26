"""Tier 1: Tests for installer.config validation functions (pure, no I/O)."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

from installer.config import (
    _lookup_iata_code_with_retry,
    _configure_token_preset_overrides,
    prompt_iata_letsmesh,
    validate_email,
    validate_meshcore_pubkey,
)


class TestValidateMeshcorePubkey:
    def test_valid_64_hex_chars(self) -> None:
        key = "A" * 64
        assert validate_meshcore_pubkey(key) == key

    def test_lowercase_returns_uppercase(self) -> None:
        key = "a" * 64
        assert validate_meshcore_pubkey(key) == "A" * 64

    def test_strips_spaces(self) -> None:
        key = "A" * 32 + " " + "B" * 32
        # After stripping space and uppercasing: 32 A's + 32 B's = 64
        assert validate_meshcore_pubkey(key) == "A" * 32 + "B" * 32

    def test_63_chars_returns_none(self) -> None:
        assert validate_meshcore_pubkey("A" * 63) is None

    def test_65_chars_returns_none(self) -> None:
        assert validate_meshcore_pubkey("A" * 65) is None

    def test_non_hex_g_returns_none(self) -> None:
        assert validate_meshcore_pubkey("G" * 64) is None

    def test_non_hex_z_returns_none(self) -> None:
        assert validate_meshcore_pubkey("Z" * 64) is None

    def test_empty_string_returns_none(self) -> None:
        assert validate_meshcore_pubkey("") is None

    def test_all_zeros_valid(self) -> None:
        assert validate_meshcore_pubkey("0" * 64) == "0" * 64

    def test_all_fs_valid(self) -> None:
        assert validate_meshcore_pubkey("F" * 64) == "F" * 64

    def test_mixed_hex_valid(self) -> None:
        key = "0123456789ABCDEFabcdef0123456789ABCDEF0123456789abcdef0123456789"
        result = validate_meshcore_pubkey(key)
        assert result is not None
        assert result == key.replace(" ", "").upper()


class TestValidateEmail:
    def test_basic_valid(self) -> None:
        assert validate_email("user@example.com") == "user@example.com"

    def test_uppercase_lowercased(self) -> None:
        assert validate_email("USER@EXAMPLE.COM") == "user@example.com"

    def test_missing_at_returns_none(self) -> None:
        assert validate_email("userexample.com") is None

    def test_missing_dot_in_domain_returns_none(self) -> None:
        assert validate_email("user@examplecom") is None

    def test_starts_with_dot_returns_none(self) -> None:
        assert validate_email(".user@example.com") is None

    def test_starts_with_at_returns_none(self) -> None:
        assert validate_email("@example.com") is None

    def test_ends_with_dot_returns_none(self) -> None:
        assert validate_email("user@example.com.") is None

    def test_ends_with_at_returns_none(self) -> None:
        assert validate_email("user@") is None

    def test_double_dot_returns_none(self) -> None:
        assert validate_email("user@example..com") is None

    def test_space_returns_none(self) -> None:
        assert validate_email("us er@example.com") is None

    def test_empty_local_returns_none(self) -> None:
        # "@example.com" starts with @, caught by starts_with check
        assert validate_email("@example.com") is None

    def test_domain_too_short_returns_none(self) -> None:
        # domain "b" is 1 char < 3
        assert validate_email("a@b") is None

    def test_minimal_valid_3char_domain(self) -> None:
        # a@b.c -> domain "b.c" is 3 chars, has dot — valid
        assert validate_email("a@b.c") == "a@b.c"

    def test_minimal_valid(self) -> None:
        # a@bc.d -> domain is "bc.d" (4 chars), has dot
        assert validate_email("a@bc.d") == "a@bc.d"

    def test_no_dot_in_domain_returns_none(self) -> None:
        assert validate_email("a@bc") is None


class TestPromptIataLetsmesh:
    def test_direct_code_can_be_used_when_lookup_misses(self) -> None:
        with (
            patch("installer.config.prompt_input", return_value="SEA"),
            patch("installer.config._lookup_iata_code_with_retry", return_value=(None, False)),
            patch("installer.config.prompt_yes_no", return_value=True),
        ):
            assert prompt_iata_letsmesh(script_version="test") == "SEA"

    def test_direct_code_can_be_used_when_validation_unavailable(self) -> None:
        with (
            patch("installer.config.prompt_input", return_value="SEA"),
            patch("installer.config._lookup_iata_code_with_retry", return_value=(None, True)),
            patch("installer.config.prompt_yes_no", return_value=True),
        ):
            assert prompt_iata_letsmesh(script_version="test") == "SEA"


class TestLookupIataCodeWithRetry:
    def test_retries_transient_failure_then_succeeds(self) -> None:
        with (
            patch(
                "installer.config._iata_request",
                side_effect=[urllib.error.URLError("temporary"), b'{"name": "Seattle-Tacoma"}'],
            ),
            patch("installer.config.time.sleep"),
        ):
            assert _lookup_iata_code_with_retry("SEA", attempts=2) == ("Seattle-Tacoma", False)

    def test_reports_validation_unavailable_after_retries(self) -> None:
        with (
            patch("installer.config._iata_request", side_effect=urllib.error.URLError("temporary")),
            patch("installer.config.time.sleep"),
        ):
            assert _lookup_iata_code_with_retry("SEA", attempts=2) == (None, True)


class TestTokenPresetOwnerPrompt:
    def test_shows_preset_and_current_owner_info(self, tmp_path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        preset = config_d / "10-letsmesh.toml"
        preset.write_text(
            '[[broker]]\nname = "letsmesh-us"\n[broker.auth]\nmethod = "token"\n'
            '\n[[broker]]\nname = "letsmesh-eu"\n[broker.auth]\nmethod = "token"\n'
        )
        user_toml = config_d / "99-user.toml"
        user_toml.write_text('[general]\niata = "SEA"\n')

        with (
            patch("installer.config.prompt_owner_pubkey", return_value=""),
            patch("installer.config.prompt_owner_email", return_value=""),
            patch("installer.config.prompt_allowed_companions", return_value=""),
            patch("builtins.print") as mock_print,
        ):
            _configure_token_preset_overrides(str(tmp_path))

        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        assert "Owner Info: 10-letsmesh.toml" in printed
        assert "letsmesh-us" in printed
        assert "letsmesh-eu" in printed
        assert "owner: (not set)" in printed

    def test_existing_owner_info_can_be_kept(self, tmp_path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        preset = config_d / "10-community.toml"
        preset.write_text(
            '[[broker]]\nname = "community"\n[broker.auth]\nmethod = "token"\n'
            'owner = "OLDOWNER"\nemail = "old@example.com"\n'
        )
        user_toml = config_d / "99-user.toml"
        user_toml.write_text('[general]\niata = "SEA"\n')

        with (
            patch("installer.config.prompt_yes_no", return_value=False) as mock_yes_no,
            patch("installer.config.prompt_owner_pubkey") as mock_owner,
            patch("installer.config.prompt_owner_email") as mock_email,
            patch("installer.config.prompt_allowed_companions", return_value=""),
        ):
            _configure_token_preset_overrides(str(tmp_path))

        mock_yes_no.assert_called_once()
        mock_owner.assert_not_called()
        mock_email.assert_not_called()

    def test_presets_can_get_different_owner_info(self, tmp_path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "10-a.toml").write_text(
            '[[broker]]\nname = "a"\n[broker.auth]\nmethod = "token"\n'
        )
        (config_d / "10-b.toml").write_text(
            '[[broker]]\nname = "b"\n[broker.auth]\nmethod = "token"\n'
        )
        user_toml = config_d / "99-user.toml"
        user_toml.write_text('[general]\niata = "SEA"\n')

        with (
            patch("installer.config.prompt_owner_pubkey", side_effect=["OWNERA", "OWNERB"]),
            patch("installer.config.prompt_owner_email", side_effect=["a@example.com", "b@example.com"]),
            patch("installer.config.prompt_allowed_companions", return_value=""),
        ):
            _configure_token_preset_overrides(str(tmp_path))

        content = user_toml.read_text()
        assert 'name = "a"' in content
        assert 'owner = "OWNERA"' in content
        assert 'email = "a@example.com"' in content
        assert 'name = "b"' in content
        assert 'owner = "OWNERB"' in content
        assert 'email = "b@example.com"' in content

    def test_owner_info_update_preserves_custom_broker_overrides(self, tmp_path) -> None:
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "10-letsmesh.toml").write_text(
            '[[broker]]\nname = "letsmesh-us"\n[broker.auth]\nmethod = "token"\n'
        )
        user_toml = config_d / "99-user.toml"
        user_toml.write_text(
            '[general]\niata = "SEA"\n\n'
            '[[broker]]\nname = "custom-override"\nserver = "mqtt.example.com"\n'
            'port = 1883\n[broker.auth]\nmethod = "none"\n'
        )

        with (
            patch("installer.config.prompt_owner_pubkey", return_value="OWNER"),
            patch("installer.config.prompt_owner_email", return_value="owner@example.com"),
            patch("installer.config.prompt_allowed_companions", return_value=""),
        ):
            _configure_token_preset_overrides(str(tmp_path))

        content = user_toml.read_text()
        assert 'name = "custom-override"' in content
        assert 'server = "mqtt.example.com"' in content
        assert 'port = 1883' in content
        assert 'method = "none"' in content
        assert 'name = "letsmesh-us"' in content
        assert 'owner = "OWNER"' in content
