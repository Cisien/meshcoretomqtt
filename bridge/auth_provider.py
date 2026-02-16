"""Authentication provider abstraction for MeshCore token operations."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from auth_token import create_auth_token, verify_auth_token, decode_token_payload


class AuthProvider(ABC):
    """Abstract interface for token operations."""

    @abstractmethod
    def create_token(self, public_key_hex: str, private_key_hex: str,
                     expiry_seconds: int = 3600, **claims: Any) -> str: ...

    @abstractmethod
    def verify_token(self, token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def decode_payload(self, token: str) -> dict[str, Any]: ...


class MeshCoreAuthProvider(AuthProvider):
    """Concrete auth provider delegating to meshcore-decoder CLI via auth_token.py."""

    def create_token(self, public_key_hex: str, private_key_hex: str,
                     expiry_seconds: int = 3600, **claims: Any) -> str:
        return create_auth_token(public_key_hex, private_key_hex,
                                 expiry_seconds=expiry_seconds, **claims)

    def verify_token(self, token: str, expected_public_key_hex: str | None = None) -> dict[str, Any]:
        return verify_auth_token(token, expected_public_key_hex)

    def decode_payload(self, token: str) -> dict[str, Any]:
        return decode_token_payload(token)
