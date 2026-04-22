#!/usr/bin/env python3
"""
MeshCore Auth Token Generator
Generates JWT-style authentication tokens for MQTT authentication
"""
from __future__ import annotations

import json
import base64
import hashlib
import time
import sys
from typing import Any
from ed25519_orlp import ed25519_sign, ed25519_verify

def base64url_encode(data: bytes) -> str:
    """Base64url encode without padding"""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

def create_auth_token(public_key_hex: str, private_key_hex: str, expiry_seconds: int = 3600, **claims: Any) -> str:
    """
    Create a JWT-style auth token for MeshCore MQTT authentication

    Args:
        public_key_hex: 32-byte public key in hex format
        private_key_hex: 64-byte private key in hex format (MeshCore format)
        expiry_seconds: Token expiry time in seconds (default 24 hours)
        **claims: Additional JWT claims (e.g., audience="mqtt.example.com", sub="device-123")

    Returns:
        JWT-style token string
    """
    try:
        # Normalize keys
        pubkey_bytes = bytes.fromhex(public_key_hex)
        prvkey_bytes = bytes.fromhex(private_key_hex)

        if len(pubkey_bytes) != 32:
            raise ValueError(f"Public key must be 32 bytes, got {len(pubkey_bytes)}")
        if len(prvkey_bytes) != 64:
            raise ValueError(f"Private key must be 64 bytes, got {len(prvkey_bytes)}")

        # Create header
        header = {"alg": "Ed25519", "typ": "JWT"}

        # Create payload
        iat = int(time.time())
        exp = iat + expiry_seconds

        payload = {
            "publicKey": public_key_hex.upper(),
            "iat": iat,
            "exp": exp
        }
        payload.update(claims)

        # Encode header and payload to JSON (no spaces to match Node.js)
        header_json = json.dumps(header, separators=(',', ':'))
        payload_json = json.dumps(payload, separators=(',', ':'))

        # Base64url encode
        header_encoded = base64url_encode(header_json.encode('utf-8'))
        payload_encoded = base64url_encode(payload_json.encode('utf-8'))

        # Signing input: header.payload
        signing_input = f"{header_encoded}.{payload_encoded}"

        # Sign the input
        signature_bytes = ed25519_sign(signing_input.encode('utf-8'), pubkey_bytes, prvkey_bytes)
        signature_hex = signature_bytes.hex().upper()

        return f"{signing_input}.{signature_hex}"

    except Exception as e:
        raise Exception(f"Failed to generate auth token: {str(e)}")

def verify_auth_token(token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]:
    """
    Verify a JWT-style auth token and return the payload if valid.

    Args:
        token: JWT-style token string (header.payload.signature)
        expected_public_key_hex: Optional - verify the token was signed by this public key

    Returns:
        Decoded payload dict if valid

    Raises:
        Exception if token is invalid, expired, or signature verification fails
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise Exception("Invalid token format: expected 3 parts")

        header_encoded, payload_encoded, signature_hex = parts

        # Decode payload to get the public key
        payload = decode_token_payload(token)

        token_pubkey_hex = payload.get('publicKey')
        if not token_pubkey_hex:
            raise Exception("Token payload missing publicKey")

        if expected_public_key_hex and token_pubkey_hex.upper() != expected_public_key_hex.upper():
            raise Exception("Token public key does not match expected public key")

        # Verify signature
        signing_input = f"{header_encoded}.{payload_encoded}"
        pubkey_bytes = bytes.fromhex(token_pubkey_hex)
        signature_bytes = bytes.fromhex(signature_hex)

        if not ed25519_verify(signature_bytes, signing_input.encode('utf-8'), pubkey_bytes):
            raise Exception("Invalid signature")

        # Check expiration
        now = int(time.time())
        if 'exp' in payload and now > payload['exp']:
            raise Exception("Token has expired")

        return payload

    except Exception as e:
        if "Token verification failed" in str(e) or "expired" in str(e).lower() or "Invalid" in str(e):
            raise
        raise Exception(f"Token verification error: {str(e)}")


def decode_token_payload(token: str) -> dict[str, Any]:
    """
    Decode a JWT payload without verifying the signature.
    Useful for extracting claims before full verification.

    Args:
        token: JWT-style token string (header.payload.signature)

    Returns:
        Decoded payload dict

    Raises:
        Exception if token format is invalid
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise Exception(f"Invalid token format: expected 3 parts, got {len(parts)}")

        # Decode the payload (second part)
        payload_b64 = parts[1]
        # Add padding if needed
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding

        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes.decode('utf-8'))

    except json.JSONDecodeError as e:
        raise Exception(f"Failed to decode token payload: {e}")
    except Exception as e:
        if "Invalid token format" in str(e):
            raise
        raise Exception(f"Token decode error: {str(e)}")


def read_private_key_file(filepath: str) -> str:
    """Read private key from file (64-byte hex format)"""
    try:
        with open(filepath, 'r') as f:
            key = f.read().strip()
            key = ''.join(key.split())
            if len(key) != 128:  # 64 bytes = 128 hex chars
                raise ValueError(f"Invalid private key length: {len(key)} (expected 128)")
            int(key, 16)
            return key
    except FileNotFoundError:
        raise Exception(f"Private key file not found: {filepath}")
    except ValueError as e:
        raise Exception(f"Invalid private key format: {str(e)}")

if __name__ == "__main__":
    # Test/CLI usage
    if len(sys.argv) < 3:
        print("Usage: python auth_token.py <public_key_hex> <private_key_hex_or_file>")
        sys.exit(1)

    public_key = sys.argv[1]
    private_key_input = sys.argv[2]

    if len(private_key_input) < 128:
        try:
            private_key = read_private_key_file(private_key_input)
            print(f"Loaded private key from: {private_key_input}")
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        private_key = private_key_input

    try:
        token = create_auth_token(public_key, private_key)
        print(f"Generated token: {token}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
