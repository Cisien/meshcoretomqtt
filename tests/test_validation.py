"""Tier 1: Tests for installer.config validation functions (pure, no I/O)."""

from __future__ import annotations

from installer.config import validate_email, validate_meshcore_pubkey


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
