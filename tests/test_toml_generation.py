"""Tier 1: Tests for TOML escaping, formatting, and URL generation (pure, no I/O)."""

from __future__ import annotations

from installer.config import _companions_to_toml_array, _iata_api_url, toml_escape


class TestTomlEscape:
    def test_plain_string_unchanged(self) -> None:
        assert toml_escape("hello world") == "hello world"

    def test_backslash_doubled(self) -> None:
        assert toml_escape("path\\to\\file") == "path\\\\to\\\\file"

    def test_double_quote_escaped(self) -> None:
        assert toml_escape('say "hello"') == 'say \\"hello\\"'

    def test_both_backslash_and_quote(self) -> None:
        assert toml_escape('a\\b"c') == 'a\\\\b\\"c'

    def test_empty_string(self) -> None:
        assert toml_escape("") == ""


class TestCompanionsToTomlArray:
    def test_empty_string(self) -> None:
        assert _companions_to_toml_array("") == "[]"

    def test_single_key(self) -> None:
        result = _companions_to_toml_array("KEY1")
        assert result == '["KEY1"]'

    def test_two_keys(self) -> None:
        result = _companions_to_toml_array("KEY1,KEY2")
        assert result == '["KEY1", "KEY2"]'

    def test_whitespace_trimmed(self) -> None:
        result = _companions_to_toml_array("  KEY1 , KEY2  ")
        assert result == '["KEY1", "KEY2"]'

    def test_trailing_comma(self) -> None:
        result = _companions_to_toml_array("KEY1,KEY2,")
        assert result == '["KEY1", "KEY2"]'

    def test_only_whitespace_and_commas(self) -> None:
        assert _companions_to_toml_array(", , ,") == "[]"


class TestIataApiUrl:
    def test_basic_params(self) -> None:
        url = _iata_api_url("search=Seattle", "1.0.0")
        assert url == "https://api.letsmesh.net/api/iata?search=Seattle&source=installer-1.0.0"

    def test_default_version(self) -> None:
        url = _iata_api_url("code=SEA")
        assert "&source=installer-unknown" in url

    def test_params_preserved(self) -> None:
        url = _iata_api_url("code=LAX", "test")
        assert "code=LAX" in url
        assert "source=installer-test" in url
