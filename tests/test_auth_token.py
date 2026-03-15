"""Tests for native Python Ed25519 JWT implementation in auth_token.py."""
from __future__ import annotations

import time
import pytest

cryptography = pytest.importorskip("cryptography", reason="cryptography package not installed")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

from auth_token import (
    _create_auth_token_python,
    _verify_auth_token_python,
    decode_token_payload,
)


def make_keypair() -> tuple[str, str]:
    """Generate a random Ed25519 keypair in MeshCore expanded-key hex format.

    MeshCore stores private keys as the expanded Ed25519 key (SHA-512 of seed,
    clamped) — NOT as seed || public_key.  bytes 0-31 are the clamped scalar,
    bytes 32-63 are the nonce prefix.
    """
    import hashlib
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    expanded = bytearray(hashlib.sha512(seed).digest())
    expanded[0] &= 248
    expanded[31] &= 63
    expanded[31] |= 64
    return pub.hex().upper(), bytes(expanded).hex().upper()


class TestCreateToken:
    def test_returns_three_part_jwt(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv)
        assert token.count('.') == 2

    def test_payload_contains_public_key(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv)
        payload = decode_token_payload(token)
        assert payload['publicKey'] == pub.upper()

    def test_payload_contains_expiry(self):
        pub, priv = make_keypair()
        before = int(time.time())
        token = _create_auth_token_python(pub, priv, expiry_seconds=3600)
        payload = decode_token_payload(token)
        assert payload['exp'] >= before + 3600

    def test_payload_contains_iat(self):
        pub, priv = make_keypair()
        before = int(time.time())
        token = _create_auth_token_python(pub, priv)
        payload = decode_token_payload(token)
        assert payload['iat'] >= before

    def test_extra_claims_included(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv, aud="test.broker", owner="AABB")
        payload = decode_token_payload(token)
        assert payload['aud'] == "test.broker"
        assert payload['owner'] == "AABB"

    def test_header_alg(self):
        import base64, json
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv)
        header_b64 = token.split('.')[0]
        padding = 4 - len(header_b64) % 4
        if padding != 4:
            header_b64 += '=' * padding
        header = json.loads(base64.urlsafe_b64decode(header_b64))
        assert header['alg'] == 'Ed25519'


class TestVerifyToken:
    def test_valid_token_roundtrip(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv, command="get name")
        payload = _verify_auth_token_python(token, pub)
        assert payload['publicKey'] == pub.upper()
        assert payload['command'] == "get name"

    def test_verify_uses_payload_public_key_when_not_specified(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv)
        payload = _verify_auth_token_python(token)
        assert payload['publicKey'] == pub.upper()

    def test_rejects_tampered_payload(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv)
        header, payload_b64, sig = token.split('.')
        # Flip one char in the payload
        tampered = payload_b64[:-1] + ('A' if payload_b64[-1] != 'A' else 'B')
        with pytest.raises(Exception):
            _verify_auth_token_python(f"{header}.{tampered}.{sig}", pub)

    def test_rejects_wrong_public_key(self):
        pub, priv = make_keypair()
        other_pub, _ = make_keypair()
        token = _create_auth_token_python(pub, priv)
        with pytest.raises(Exception):
            _verify_auth_token_python(token, other_pub)

    def test_rejects_expired_token(self):
        pub, priv = make_keypair()
        token = _create_auth_token_python(pub, priv, expiry_seconds=-1)
        with pytest.raises(Exception, match="expired"):
            _verify_auth_token_python(token, pub)
