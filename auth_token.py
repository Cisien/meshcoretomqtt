#!/usr/bin/env python3
"""
MeshCore Auth Token Generator

Creates and verifies Ed25519-signed JWTs for MQTT authentication.
Uses a pure-Python Ed25519 implementation for signing, and the
`cryptography` package for verification when available; falls back to the
`meshcore-decoder` Node.js CLI if not installed.

Token format: standard JWT (header.payload.signature)
  header:  {"alg":"Ed25519","typ":"JWT"}
  payload: {"publicKey": "<hex>", "iat": <unix>, "exp": <unix>, ...claims}
  sig:     raw Ed25519 signature over base64url(header).base64url(payload)
           encoded as uppercase hex (not base64url)

MeshCore private key format: 64 bytes = expanded Ed25519 key
  bytes  0-31: SHA-512(seed)[0:32] with clamping — the scalar used for signing
  bytes 32-63: SHA-512(seed)[32:64] — the nonce prefix
"""
from __future__ import annotations

import base64
import json
import time
import subprocess
import sys
from typing import Any


def base64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')


def base64url_decode(s: str) -> bytes:
    """Base64url decode, adding padding as needed."""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return base64.urlsafe_b64decode(s)


# ---------------------------------------------------------------------------
# Pure-Python Ed25519 signing with MeshCore expanded key format
# ---------------------------------------------------------------------------

def _ed25519_sign_expanded(expanded_key: bytes, public_key: bytes, message: bytes) -> bytes:
    """Sign using MeshCore's 64-byte expanded Ed25519 private key.

    expanded_key[0:32]: clamped scalar (used directly for signing)
    expanded_key[32:64]: nonce prefix (hashed with message to generate r)

    Uses only hashlib; no third-party dependencies required.
    """
    import hashlib

    P = 2**255 - 19
    L = 2**252 + 27742317777372353535851937790883648493

    def _sha512(*parts: bytes) -> bytes:
        h = hashlib.sha512()
        for p in parts:
            h.update(p)
        return h.digest()

    def _inv(x: int) -> int:
        return pow(x, P - 2, P)

    d = -121665 * _inv(121666) % P

    def _recover_x(y: int, sign: int) -> int:
        y2 = y * y % P
        x2 = (y2 - 1) * _inv(d * y2 + 1) % P
        if x2 == 0:
            return 0
        x = pow(x2, (P + 3) // 8, P)
        if (x * x - x2) % P != 0:
            x = x * pow(2, (P - 1) // 4, P) % P
        if x & 1 != sign:
            x = P - x
        return x

    Gy = 4 * _inv(5) % P
    Gx = _recover_x(Gy, 0)
    G = (Gx, Gy, 1, Gx * Gy % P)  # Extended coordinates (X, Y, Z, T)

    def _add(pt1: tuple, pt2: tuple) -> tuple:
        x1, y1, z1, t1 = pt1
        x2, y2, z2, t2 = pt2
        a = (y1 - x1) * (y2 - x2) % P
        b = (y1 + x1) * (y2 + x2) % P
        c = t1 * 2 * d * t2 % P
        e_val = z1 * 2 * z2 % P
        e = b - a
        f = e_val - c
        g = e_val + c
        h = b + a
        return (e * f % P, g * h % P, f * g % P, e * h % P)

    def _mul(s: int, pt: tuple) -> tuple:
        q = (0, 1, 1, 0)  # neutral element
        while s:
            if s & 1:
                q = _add(q, pt)
            pt = _add(pt, pt)
            s >>= 1
        return q

    def _encode(pt: tuple) -> bytes:
        zi = _inv(pt[2])
        x = pt[0] * zi % P
        y = pt[1] * zi % P
        b = bytearray(y.to_bytes(32, 'little'))
        if x & 1:
            b[31] |= 0x80
        return bytes(b)

    nonce_prefix = expanded_key[32:64]
    scalar_a = int.from_bytes(expanded_key[0:32], 'little')
    r = int.from_bytes(_sha512(nonce_prefix, message), 'little') % L
    R = _encode(_mul(r, G))
    k = int.from_bytes(_sha512(R, public_key, message), 'little') % L
    S = (r + k * scalar_a) % L
    return R + S.to_bytes(32, 'little')


# ---------------------------------------------------------------------------
# Python-native Ed25519 implementation
# ---------------------------------------------------------------------------

def _create_auth_token_python(public_key_hex: str, private_key_hex: str,
                               expiry_seconds: int = 3600, **claims: Any) -> str:
    now = int(time.time())
    header_b64 = base64url_encode(json.dumps({"alg": "Ed25519", "typ": "JWT"}, separators=(',', ':')).encode())
    payload_b64 = base64url_encode(json.dumps(
        {"publicKey": public_key_hex.upper(), "iat": now, "exp": now + expiry_seconds, **claims},
        separators=(',', ':'),
    ).encode())

    signing_input = f"{header_b64}.{payload_b64}".encode()

    # MeshCore 64-byte expanded key: bytes 0-31 = scalar, bytes 32-63 = nonce prefix
    expanded_key = bytes.fromhex(private_key_hex)
    public_key = bytes.fromhex(public_key_hex)
    signature = _ed25519_sign_expanded(expanded_key, public_key, signing_input)

    # meshcore-decoder encodes the signature as uppercase hex, not base64url
    return f"{header_b64}.{payload_b64}.{signature.hex().upper()}"


def _verify_auth_token_python(token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature

    parts = token.split('.')
    if len(parts) != 3:
        raise Exception(f"Invalid token format: expected 3 parts, got {len(parts)}")

    header_b64, payload_b64, signature_b64 = parts

    payload = json.loads(base64url_decode(payload_b64))

    if payload.get('exp', 0) < int(time.time()):
        raise Exception("Token has expired")

    pub_key_hex = expected_public_key_hex or payload.get('publicKey', '')
    if not pub_key_hex:
        raise Exception("No public key available for verification")

    signing_input = f"{header_b64}.{payload_b64}".encode()
    # Signature may be hex-encoded (meshcore-decoder format) or base64url (standard JWT)
    try:
        if all(c in '0123456789ABCDEFabcdef' for c in signature_b64) and len(signature_b64) == 128:
            sig_bytes = bytes.fromhex(signature_b64)
        else:
            sig_bytes = base64url_decode(signature_b64)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_key_hex[:64])).verify(
            sig_bytes, signing_input
        )
    except InvalidSignature:
        raise Exception("Token verification failed: invalid signature")

    return payload


# ---------------------------------------------------------------------------
# meshcore-decoder CLI fallback
# ---------------------------------------------------------------------------

def _create_auth_token_cli(public_key_hex: str, private_key_hex: str,
                            expiry_seconds: int = 3600, **claims: Any) -> str:
    cmd = ['meshcore-decoder', 'auth-token', public_key_hex, private_key_hex, '-e', str(expiry_seconds)]
    if claims:
        cmd.extend(['-c', json.dumps(claims)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise Exception("Token generation timed out")
    except FileNotFoundError:
        raise Exception("meshcore-decoder CLI not found. Install with: npm install -g @michaelhart/meshcore-decoder")
    if result.returncode != 0:
        raise Exception(f"meshcore-decoder error: {result.stderr}")
    token = result.stdout.strip()
    if not token or token.count('.') != 2:
        raise Exception(f"Invalid token format from CLI: {token}")
    return token


def _verify_auth_token_cli(token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]:
    cmd = ['meshcore-decoder', 'verify-token', token, '--json']
    if expected_public_key_hex:
        cmd.extend(['-p', expected_public_key_hex])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise Exception("Token verification timed out")
    except FileNotFoundError:
        raise Exception("meshcore-decoder CLI not found. Install with: npm install -g @michaelhart/meshcore-decoder")
    if result.returncode != 0:
        raise Exception(f"Token verification failed: {result.stderr.strip() or 'verification failed'}")
    output = json.loads(result.stdout.strip())
    if not output.get('valid', False):
        raise Exception(f"Token verification failed: {output.get('error', 'unknown error')}")
    if output.get('expired', False):
        raise Exception("Token has expired")
    return output.get('payload', {})


# ---------------------------------------------------------------------------
# Public API — tries Python, falls back to CLI
# ---------------------------------------------------------------------------

def create_auth_token(public_key_hex: str, private_key_hex: str,
                      expiry_seconds: int = 3600, **claims: Any) -> str:
    """
    Create a JWT-style auth token signed with the device's Ed25519 private key.

    Uses a pure-Python Ed25519 implementation (no external dependencies).
    Falls back to the meshcore-decoder CLI on unexpected errors.
    """
    try:
        return _create_auth_token_python(public_key_hex, private_key_hex, expiry_seconds, **claims)
    except Exception:
        return _create_auth_token_cli(public_key_hex, private_key_hex, expiry_seconds, **claims)


def verify_auth_token(token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]:
    """
    Verify a JWT auth token and return the payload if valid.

    Uses the `cryptography` package when available; falls back to the
    meshcore-decoder CLI otherwise.
    """
    try:
        return _verify_auth_token_python(token, expected_public_key_hex)
    except ImportError:
        return _verify_auth_token_cli(token, expected_public_key_hex)


def decode_token_payload(token: str) -> dict[str, Any]:
    """Decode JWT payload without verifying the signature."""
    parts = token.split('.')
    if len(parts) != 3:
        raise Exception(f"Invalid token format: expected 3 parts, got {len(parts)}")
    return json.loads(base64url_decode(parts[1]))


def read_private_key_file(filepath: str) -> str:
    """Read private key from file (64-byte hex format)."""
    try:
        with open(filepath, 'r') as f:
            key = ''.join(f.read().split())
            if len(key) != 128:
                raise ValueError(f"Invalid private key length: {len(key)} (expected 128)")
            int(key, 16)
            return key
    except FileNotFoundError:
        raise Exception(f"Private key file not found: {filepath}")
    except ValueError as e:
        raise Exception(f"Invalid private key format: {str(e)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate a MeshCore Ed25519 JWT auth token")
    parser.add_argument("public_key", help="Public key (hex)")
    parser.add_argument("private_key", help="Private key (hex) or path to key file")
    parser.add_argument("-e", "--expiry", type=int, default=3600, help="Expiry in seconds (default: 3600)")
    parser.add_argument("-c", "--claims", default=None, help="Extra claims as JSON object")
    args = parser.parse_args()

    if len(args.private_key) < 128:
        try:
            private_key = read_private_key_file(args.private_key)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        private_key = args.private_key

    extra_claims: dict[str, Any] = {}
    if args.claims:
        try:
            extra_claims = json.loads(args.claims)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON for --claims: {e}")
            sys.exit(1)

    try:
        token = create_auth_token(args.public_key, private_key, expiry_seconds=args.expiry, **extra_claims)
        print(token)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
