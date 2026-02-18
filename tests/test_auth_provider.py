"""Tests for AuthProvider abstractions."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bridge.auth_provider import MeshCoreAuthProvider
from tests.fakes import FakeAuthProvider


class TestMeshCoreAuthProviderDelegation:
    """Verify MeshCoreAuthProvider delegates to auth_token functions."""

    @patch('bridge.auth_provider.create_auth_token', return_value="header.payload.signature")
    def test_delegates_create(self, mock_create):
        provider = MeshCoreAuthProvider()
        result = provider.create_token("AA" * 32, "BB" * 64, expiry_seconds=3600, aud="test")
        mock_create.assert_called_once_with("AA" * 32, "BB" * 64, expiry_seconds=3600, aud="test")
        assert result == "header.payload.signature"

    @patch('bridge.auth_provider.verify_auth_token', return_value={"publicKey": "AA" * 32})
    def test_delegates_verify(self, mock_verify):
        provider = MeshCoreAuthProvider()
        result = provider.verify_token("some.jwt.token", "AA" * 32)
        mock_verify.assert_called_once_with("some.jwt.token", "AA" * 32)
        assert result == {"publicKey": "AA" * 32}

    @patch('bridge.auth_provider.decode_token_payload', return_value={"sub": "test"})
    def test_delegates_decode(self, mock_decode):
        provider = MeshCoreAuthProvider()
        result = provider.decode_payload("some.jwt.token")
        mock_decode.assert_called_once_with("some.jwt.token")
        assert result == {"sub": "test"}


class TestFakeAuthProvider:
    def test_create_returns_fake_token(self):
        provider = FakeAuthProvider()
        token = provider.create_token("AA" * 32, "BB" * 64, expiry_seconds=60)
        assert token.count('.') == 2

    def test_decode_roundtrip(self):
        provider = FakeAuthProvider()
        token = provider.create_token("AA" * 32, "BB" * 64, expiry_seconds=60, command="test")
        payload = provider.decode_payload(token)
        assert payload['publicKey'] == "AA" * 32
        assert payload['command'] == "test"

    def test_verify_succeeds_with_valid_key(self):
        provider = FakeAuthProvider(valid_keys={"AA" * 32})
        token = provider.create_token("AA" * 32, "BB" * 64)
        result = provider.verify_token(token, "AA" * 32)
        assert result['publicKey'] == "AA" * 32

    def test_verify_fails_with_invalid_key(self):
        provider = FakeAuthProvider(valid_keys={"AA" * 32})
        token = provider.create_token("AA" * 32, "BB" * 64)
        with pytest.raises(Exception, match="Verification failed"):
            provider.verify_token(token, "CC" * 32)

    def test_verify_fails_when_configured(self):
        provider = FakeAuthProvider(should_fail_verify=True)
        token = provider.create_token("AA" * 32, "BB" * 64)
        with pytest.raises(Exception, match="Verification failed"):
            provider.verify_token(token)
