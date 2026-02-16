#!/usr/bin/env python3
"""
MeshCore Auth Token Generator
Generates JWT-style authentication tokens for MQTT authentication
"""
import json
import base64
import hashlib
import time
import subprocess
import sys

def base64url_encode(data: bytes) -> str:
    """Base64url encode without padding"""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

def create_auth_token(public_key_hex: str, private_key_hex: str, expiry_seconds: int = 3600, **claims) -> str:
    """
    Create a JWT-style auth token for MeshCore MQTT authentication
    
    Uses the meshcore-decoder CLI tool.
    
    Args:
        public_key_hex: 32-byte public key in hex format
        private_key_hex: 64-byte private key in hex format (MeshCore format)
        expiry_seconds: Token expiry time in seconds (default 24 hours)
        **claims: Additional JWT claims (e.g., audience="mqtt.example.com", sub="device-123")
    
    Returns:
        JWT-style token string
    """
    try:
        cmd = ['meshcore-decoder', 'auth-token', public_key_hex, private_key_hex, '-e', str(expiry_seconds)]
        
        if claims:
            claims_json = json.dumps(claims)
            cmd.extend(['-c', claims_json])
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            raise Exception(f"meshcore-decoder error: {result.stderr}")
        
        token = result.stdout.strip()
        if not token or token.count('.') != 2:
            raise Exception(f"Invalid token format: {token}")
        
        return token
        
    except subprocess.TimeoutExpired:
        raise Exception("Token generation timed out")
    except FileNotFoundError:
        raise Exception("meshcore-decoder CLI not found. Install it with: npm install -g @michaelhart/meshcore-decoder")
    except Exception as e:
        raise Exception(f"Failed to generate auth token: {str(e)}")

def verify_auth_token(token: str, expected_public_key_hex: str = None) -> dict:
    """
    Verify a JWT-style auth token and return the payload if valid.
    
    Uses the meshcore-decoder CLI tool for signature verification.
    
    Args:
        token: JWT-style token string (header.payload.signature)
        expected_public_key_hex: Optional - verify the token was signed by this public key
    
    Returns:
        Decoded payload dict if valid
        
    Raises:
        Exception if token is invalid, expired, or signature verification fails
    """
    try:
        cmd = ['meshcore-decoder', 'verify-token', token, '--json']
        
        if expected_public_key_hex:
            cmd.extend(['-p', expected_public_key_hex])
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Verification failed"
            raise Exception(f"Token verification failed: {error_msg}")
        
        # The CLI outputs JSON with {valid: true/false, payload: {...}, ...} when --json is used
        output_json = result.stdout.strip()
        if not output_json:
            raise Exception("Empty output from verification")
        
        output = json.loads(output_json)
        
        if not output.get('valid', False):
            error = output.get('error', 'Unknown verification error')
            raise Exception(f"Token verification failed: {error}")
        
        # Check if token is expired (CLI includes this info)
        if output.get('expired', False):
            raise Exception("Token has expired")
        
        return output.get('payload', {})
        
    except subprocess.TimeoutExpired:
        raise Exception("Token verification timed out")
    except FileNotFoundError:
        raise Exception("meshcore-decoder CLI not found. Install it with: npm install -g @michaelhart/meshcore-decoder")
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse verification output: {e}")
    except Exception as e:
        if "Token verification failed" in str(e) or "meshcore-decoder" in str(e) or "expired" in str(e).lower():
            raise
        raise Exception(f"Token verification error: {str(e)}")


def decode_token_payload(token: str) -> dict:
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
